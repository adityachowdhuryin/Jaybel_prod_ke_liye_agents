"""Read-only cost query helpers with dual backend support.

Backends:
- BigQuery Billing Export table (preferred when configured)
- PostgreSQL `cloud_costs` table (deprecated: only used as a last-resort fallback
  in ``auto`` mode when BigQuery is unavailable, or when ``COST_DATA_SOURCE=postgres``)
"""

from __future__ import annotations

import calendar
import json
import os
import re
from dataclasses import dataclass, replace
from datetime import date, timedelta
from decimal import Decimal
from typing import Any

from google.cloud import bigquery
import psycopg

from .billing_context_router import (
    ResolvedCostContext,
    llm_context_router_usable,
    normalize_bq_target,
    resolve_cost_context,
)
from .billing_llm_sql import (
    google_ai_configured,
    gcp_billing_sql_target,
    llm_sql_usable,
    run_llm_cost_sql_query,
    vertex_available,
    workflow_view_sql_target,
)
from .bq_schema_digest import build_dual_table_schema_digest
from .workflow_bq_env import workflow_raw_table_name, workflow_table_configured, workflow_table_fqn

DATABASE_URL = os.environ.get(
    "DATABASE_URL",
    "postgresql://postgres:postgres@127.0.0.1:5435/postgres",
)
SOURCE_MODE = os.environ.get("COST_DATA_SOURCE", "auto").strip().lower()
BILLING_BQ_SCHEMA_MODE = os.environ.get("BILLING_BQ_SCHEMA_MODE", "raw_export").strip().lower()

# BigQuery Billing Export source (optional)
BQ_BILLING_PROJECT = os.environ.get("BQ_BILLING_PROJECT", "").strip()
BQ_BILLING_DATASET = os.environ.get("BQ_BILLING_DATASET", "").strip()
BQ_BILLING_TABLE = os.environ.get("BQ_BILLING_TABLE", "").strip()
_SCHEMA_VALUE_LIMIT = 100

DATA_SOURCE_CHOICE_PREFIX = "_DATA_SOURCE_CHOICE_:"
BILLING_PROJECT_CHOICE_PREFIX = "_BILLING_PROJECT_ID_:"


def _billing_legacy_regex_routing() -> bool:
    return os.environ.get("BILLING_LEGACY_REGEX_ROUTING", "").lower() in ("1", "true", "yes")


def _billing_deterministic_trace_total() -> bool:
    return os.environ.get("BILLING_DETERMINISTIC_TRACE_TOTAL", "").lower() in ("1", "true", "yes")


def _strip_forced_data_source_prefix(question: str) -> tuple[str, str | None]:
    forced: str | None = None
    out_lines: list[str] = []
    for line in question.splitlines():
        s = line.strip()
        low = s.lower()
        pfx = DATA_SOURCE_CHOICE_PREFIX.lower()
        if low.startswith(pfx):
            val = s[len(DATA_SOURCE_CHOICE_PREFIX) :].strip().lower()
            if val in ("gcp_billing", "gcp_workflow", "agent_cost_events"):
                forced = normalize_bq_target(val)
            continue
        out_lines.append(line)
    return "\n".join(out_lines).strip(), forced


def _strip_forced_billing_project_prefix(question: str) -> tuple[str, str | None]:
    forced: str | None = None
    out_lines: list[str] = []
    pfx_len = len(BILLING_PROJECT_CHOICE_PREFIX)
    for line in question.splitlines():
        s = line.strip()
        low = s.lower()
        if low.startswith(BILLING_PROJECT_CHOICE_PREFIX.lower()):
            val = s[pfx_len:].strip()
            if val:
                forced = val
            continue
        out_lines.append(line)
    return "\n".join(out_lines).strip(), forced


def get_connection():
    return psycopg.connect(DATABASE_URL)


_MONTH_NAMES: dict[str, int] = {
    "january": 1,
    "jan": 1,
    "february": 2,
    "feb": 2,
    "march": 3,
    "mar": 3,
    "april": 4,
    "apr": 4,
    "may": 5,
    "june": 6,
    "jun": 6,
    "july": 7,
    "jul": 7,
    "august": 8,
    "aug": 8,
    "september": 9,
    "sep": 9,
    "sept": 9,
    "october": 10,
    "oct": 10,
    "november": 11,
    "nov": 11,
    "december": 12,
    "dec": 12,
}


def _month_bounds(year: int, month: int) -> tuple[date, date]:
    last = calendar.monthrange(year, month)[1]
    return date(year, month, 1), date(year, month, last)


def _last_user_utterance(message: str) -> str:
    if not re.search(r"(?i)multi-turn conversation", message):
        return message.strip()
    parts = re.split(r"(?im)^USER:\s*", message)
    if len(parts) < 2:
        return message.strip()
    last = parts[-1].strip()
    last = re.split(r"(?im)^ASSISTANT:\s*", last, maxsplit=1)[0].strip()
    return last if last else message.strip()


_BOGUS_BILLING_PROJECT_TOKENS = frozenset(
    {
        "over",
        "the",
        "last",
        "next",
        "same",
        "each",
        "all",
        "any",
        "some",
        "few",
        "per",
        "this",
        "that",
        "your",
        "our",
        "my",
        "for",
        "with",
        "from",
        "into",
        "onto",
        "about",
        "than",
        "more",
        "less",
        "broken",
        "down",
    }
)


def _mentions_prod(q: str) -> bool:
    return bool(re.search(r"(?<![-])\b(prod|production|prd)\b", q, re.I))


def _mentions_dev(q: str) -> bool:
    return bool(re.search(r"(?<![-])\b(dev|development)\b", q, re.I))


def _dev_mention_is_project_slug(q: str) -> bool:
    return bool(re.search(r"[a-z0-9][a-z0-9-]*\s*-\s*dev\s+project", q, re.I))


def _normalize_project_id_slug(raw: str) -> str:
    return re.sub(r"\s+", "", raw.strip().lower())


def _workflow_configured() -> bool:
    bp = (BQ_BILLING_PROJECT or os.environ.get("GOOGLE_CLOUD_PROJECT", "")).strip()
    ds = (BQ_BILLING_DATASET or "").strip()
    return workflow_table_configured(billing_project=bp, billing_dataset=ds)


def _workflow_table_ref() -> tuple[str, str]:
    bp = (BQ_BILLING_PROJECT or os.environ.get("GOOGLE_CLOUD_PROJECT", "")).strip()
    ds = (BQ_BILLING_DATASET or "").strip()
    fqn = workflow_table_fqn(billing_project=bp, billing_dataset=ds)
    if not fqn:
        raise RuntimeError(
            "Set BQ_WORKFLOW_TABLE (optional BQ_WORKFLOW_PROJECT / BQ_WORKFLOW_DATASET; "
            "or legacy BQ_COST_EVENTS_*; defaults to BQ_BILLING_*)."
        )
    proj = fqn.split(".", 2)[0]
    return proj, fqn


def _heuristic_bq_target(question: str) -> str:
    q = question.lower()
    if _question_signals_usage_trace_table(question):
        return "gcp_workflow"
    triggers = (
        "cost_events",
        "cost events",
        "token usage",
        "input tokens",
        "output tokens",
        "inference cost",
        "llm cost",
        "model cost",
        "gemini cost",
        "agent cost",
        "agent engine cost",
        "adk ",
        "trace_id",
        "mcp tools",
        "jsonpayload",
        "engine_id",
        "engine display",
    )
    if any(t in q for t in triggers):
        return "gcp_workflow"
    return "gcp_billing"


def _question_signals_usage_trace_table(question: str) -> bool:
    """Identifiers from workflow/runtime traces, not GCP billing SKUs."""
    if not question or not question.strip():
        return False
    ql = question.lower()
    if "demo-trace" in ql:
        return True
    if re.search(r"\bemail-\d+", ql):
        return True
    if re.search(r"\bdemo-trace-[a-z0-9_-]+\b", question, re.I):
        return True
    if re.search(r"\btrace-[a-z0-9_.]+\b", ql) and "project" not in ql:
        return True
    return False


def _extract_usage_correlation_id(question: str) -> str | None:
    """Match trace_id values (e.g. demo-trace-2, email-177…)."""
    for pattern in (
        r"(?i)\b(demo-trace-[a-z0-9_-]+)\b",
        r"\b(email-\d{6,})\b",
        r"(?i)\b(trace-[a-z0-9_-]{3,80})\b",
    ):
        m = re.search(pattern, question)
        if m:
            return m.group(1).strip()
    return None


def _asks_scalar_total(question: str) -> bool:
    q = question.lower()
    return bool(
        re.search(r"\b(total|sum|combined|aggregate)\b", q)
        or (
            re.search(r"\b(how\s+much|cost|spend|spent|price)\b", q)
            and not re.search(r"\b(breakdown|by\s+service|per\s+sku)\b", q)
        )
    )


def _trace_cost_default_window(question: str, today: date) -> tuple[date, date]:
    ql = " ".join(question.lower().split())
    if "this month" in ql or "month-to-date" in ql or "month to date" in ql:
        return date(today.year, today.month, 1), today
    if "last month" in ql:
        first_this = date(today.year, today.month, 1)
        last_prev = first_this - timedelta(days=1)
        start = date(last_prev.year, last_prev.month, 1)
        end = date(last_prev.year, last_prev.month, calendar.monthrange(last_prev.year, last_prev.month)[1])
        return start, end
    if re.search(r"\blast\s+(\d+)\s+days?\b", ql):
        n = int(re.search(r"\blast\s+(\d+)\s+days?\b", ql).group(1))
        end = today
        start = today - timedelta(days=min(max(n, 1), 366) - 1)
        return start, end
    start = _full_history_start(today)
    return start, today


def _maybe_resolve_trace_query_without_clarification(
    question: str,
    routed: ResolvedCostContext,
    today: date,
) -> ResolvedCostContext:
    """Avoid router time-window clarification for demo-trace / email-* usage lookups."""
    if not _question_signals_usage_trace_table(question):
        return routed
    if not routed.needs_clarification:
        return routed
    ms = [str(x).strip() for x in (routed.missing_slots or []) if str(x).strip()]
    if ms != ["time_window"]:
        return routed
    ws, we = _trace_cost_default_window(question, today)
    hint = routed.hint or "no explicit filters"
    hint = f"{hint}; defaulted time window for usage/trace lookup ({ws} to {we})"
    return replace(
        routed,
        needs_clarification=False,
        missing_slots=[],
        clarification_question=None,
        clarification_options=[],
        clarification_kind=None,
        clarification_priority=None,
        window_start=ws,
        window_end=we,
        hint=hint,
        bq_target="gcp_workflow",
    )


def _deterministic_trace_total_usd(
    *,
    table_ref: str,
    job_project: str,
    correlation_id: str,
    window_start: date,
    window_end: date,
) -> float:
    """Exact SUM(cost_usd) for trace_id on the workflow view (no LLM SQL)."""
    sql = (
        f"SELECT COALESCE(SUM(IFNULL(cost_usd, 0)), 0) AS total_usd "
        f"FROM `{table_ref}` "
        f"WHERE DATE(timestamp) BETWEEN @ws AND @we "
        f"AND LOWER(TRIM(CAST(trace_id AS STRING))) = LOWER(@tid)"
    )
    client = bigquery.Client(project=job_project)
    job_config = bigquery.QueryJobConfig(
        query_parameters=[
            bigquery.ScalarQueryParameter("tid", "STRING", correlation_id),
            bigquery.ScalarQueryParameter("ws", "DATE", window_start),
            bigquery.ScalarQueryParameter("we", "DATE", window_end),
        ]
    )
    rows = list(client.query(sql, job_config=job_config).result())
    if not rows:
        return 0.0
    val = rows[0].get("total_usd")
    return float(val or 0)


def _schema_question_targets_workflow_view(question: str) -> bool:
    if not _workflow_configured():
        return False
    tname = workflow_raw_table_name().lower()
    q = question.lower()
    q_compact = q.replace("-", "_").replace(" ", "_")
    if tname and tname.replace("-", "_") in q_compact:
        return True
    for needle in (
        "workflow view",
        "jaybel_prod_workflow",
        "runtime view",
        "input_tokens",
        "output_tokens",
        "trace_id",
    ):
        if needle in q:
            return True
    if "cost_events" in q_compact:
        return True
    if "cost event" in q or "usage log" in q:
        return True
    if _billing_legacy_regex_routing():
        return _heuristic_bq_target(question) == "gcp_workflow"
    return False


def _schema_table_choice(question: str) -> tuple[str, str]:
    if _schema_question_targets_workflow_view(question):
        _, ref = _workflow_table_ref()
        return ref, workflow_raw_table_name()
    table_project, ref = _bq_table_ref()
    return ref, BQ_BILLING_TABLE


def _schema_mode() -> str:
    return "clean_view" if BILLING_BQ_SCHEMA_MODE in {"clean", "clean_view"} else "raw_export"


def _service_col() -> str:
    return "service_name" if _schema_mode() == "clean_view" else "service.description"


def _project_id_col() -> str:
    return "project_id" if _schema_mode() == "clean_view" else "project.id"


def _region_col() -> str:
    return "region" if _schema_mode() == "clean_view" else "location.region"


def _project_labels_col() -> str:
    return "project_labels" if _schema_mode() == "clean_view" else "project.labels"


def _extract_gcp_project_id(question: str) -> str | None:
    def ok(raw: str | None) -> str | None:
        if not raw:
            return None
        s = _normalize_project_id_slug(raw)
        if not s:
            return None
        if s.lower() in _BOGUS_BILLING_PROJECT_TOKENS:
            return None
        return s

    ql = question.strip()
    m_slug = re.search(
        r"(?i)\b([a-z][a-z0-9]*(?:\s*-\s*[a-z0-9]+)+)\s+project\b",
        ql,
    )
    if m_slug:
        return ok(m_slug.group(1))
    m_for = re.search(
        r"(?i)\b(?:for|in)\s+([a-z][a-z0-9]*(?:\s*-\s*[a-z0-9]+)+)\b",
        ql,
    )
    if m_for:
        return ok(m_for.group(1))
    patterns = (
        r"(?i)in\s+the\s+([a-z][a-z0-9-]{1,62})\s+project\b",
        r"(?i)\bproject\s+([a-z][a-z0-9]*(?:-[a-z0-9-]+)+)\b",
        r"(?i)\b([a-z][a-z0-9]*(?:-[a-z0-9-]+)+)\s+project\b",
        r"(?i)\b([a-z][a-z0-9-]{3,62})\s+project\b",
    )
    for p in patterns:
        m = re.search(p, ql)
        if m:
            hit = ok(m.group(1))
            if hit:
                return hit
    return None


def _parse_time_period(question: str, q_lower: str, today: date) -> tuple[date | None, date | None, list[str]]:
    notes: list[str] = []

    m_days = re.search(
        r"\b(?:(?:over|in|during|for)\s+the\s+)?(?:last|past)\s+(\d{1,2})\s+days?\b",
        q_lower,
    )
    if m_days:
        n = min(max(int(m_days.group(1)), 1), 366)
        end = today
        start = today - timedelta(days=n - 1)
        notes.append(f"last {n} days ({start} to {end})")
        return start, end, notes

    if re.search(r"\byesterday\b", q_lower):
        y = today - timedelta(days=1)
        notes.append(f"yesterday ({y})")
        return y, y, notes

    _mo = (
        r"january|february|march|april|may|june|july|august|september|october|november|december|"
        r"jan|feb|mar|apr|jun|jul|aug|sep|sept|oct|nov|dec"
    )
    m_dom = re.search(rf"\b(\d{{1,2}})(?:st|nd|rd|th)?\s+(?:of\s+)?({_mo})\s+(\d{{4}})\b", q_lower)
    if m_dom:
        dom = int(m_dom.group(1))
        month = _MONTH_NAMES[m_dom.group(2)]
        year = int(m_dom.group(3))
        if 1 <= dom <= 31:
            try:
                d_only = date(year, month, dom)
                notes.append(f"filtering date={d_only}")
                return d_only, d_only, notes
            except ValueError:
                pass
    m_mdy = re.search(
        rf"\b({_mo})\s+(\d{{1,2}})(?:st|nd|rd|th)?,?\s+(\d{{4}})\b",
        q_lower,
    )
    if m_mdy:
        month = _MONTH_NAMES[m_mdy.group(1)]
        dom = int(m_mdy.group(2))
        year = int(m_mdy.group(3))
        if 1 <= dom <= 31:
            try:
                d_only = date(year, month, dom)
                notes.append(f"filtering date={d_only}")
                return d_only, d_only, notes
            except ValueError:
                pass

    m = re.search(
        r"(?:for\s+)?(?:the\s+)?(?:entire\s+month\s+of\s+)?"
        r"(january|february|march|april|may|june|july|august|september|october|november|december|"
        r"jan|feb|mar|apr|jun|jul|aug|sep|sept|oct|nov|dec)\s+(\d{4})\b",
        q_lower,
    )
    if m:
        month = _MONTH_NAMES[m.group(1)]
        year = int(m.group(2))
        start, end = _month_bounds(year, month)
        notes.append(f"date range {start} to {end}")
        return start, end, notes

    if re.search(r"\bthis\s+month\b", q_lower):
        start = date(today.year, today.month, 1)
        notes.append("this month (month-to-date)")
        return start, today, notes

    if re.search(r"\blast\s+month\b", q_lower):
        first_this = date(today.year, today.month, 1)
        last_prev = first_this - timedelta(days=1)
        start, end = _month_bounds(last_prev.year, last_prev.month)
        notes.append(f"last month ({start} to {end})")
        return start, end, notes

    if re.search(r"\blast\s+week\b", q_lower):
        start_this_week = today - timedelta(days=today.weekday())
        end_last = start_this_week - timedelta(days=1)
        start_last = end_last - timedelta(days=6)
        notes.append(f"last calendar week ({start_last} to {end_last})")
        return start_last, end_last, notes

    if re.search(r"\bthis\s+week\b", q_lower):
        start_this_week = today - timedelta(days=today.weekday())
        notes.append("this week (week-to-date)")
        return start_this_week, today, notes

    date_match = re.search(r"\b(20\d{2}-\d{2}-\d{2})\b", question)
    if date_match:
        d = date.fromisoformat(date_match.group(1))
        notes.append(f"filtering date={d}")
        return d, d, notes

    return None, None, notes


def _looks_like_gcp_region(token: str) -> bool:
    sl = token.lower().strip()
    if not sl or len(sl) > 32:
        return False
    return bool(re.match(r"^[a-z]{2,}-[a-z0-9-]+\d$", sl)) or bool(
        re.match(r"^[a-z]{2,}-[a-z0-9]+-[a-z0-9]+\d$", sl)
    )


def _extract_billing_region(question: str) -> str | None:
    q = (
        question.strip()
        .replace("\u2018", "'")
        .replace("\u2019", "'")
        .replace("\u201c", '"')
        .replace("\u201d", '"')
    )
    patterns = (
        r"(?i)['\"]([a-z0-9-]+)['\"]\s+region\b",
        r"(?i)\bin\s+the\s+['\"]([a-z0-9-]+)['\"]\s+region\b",
        r"(?i)\bregion\s+['\"]([a-z0-9-]+)['\"]",
    )
    for p in patterns:
        m = re.search(p, q)
        if m and _looks_like_gcp_region(m.group(1)):
            return m.group(1).lower()
    return None


def _mentions_till_now(q: str) -> bool:
    t = " ".join(q.lower().split())
    return bool(
        ("till now" in t)
        or ("until now" in t)
        or ("till date" in t)
        or ("to date" in t)
        or ("so far" in t)
    )


def _full_history_start(today: date) -> date:
    raw = os.environ.get("BILLING_FULL_HISTORY_START_DATE", "").strip()
    if raw:
        try:
            return date.fromisoformat(raw)
        except ValueError:
            pass
    return date(today.year, 1, 1)


@dataclass(frozen=True)
class CostQueryFilters:
    env: str | None
    svc: str | None
    billing_project_id: str | None
    billing_region: str | None
    period_start: date | None
    period_end: date | None
    wants_total: bool
    wants_top: bool
    hint: str

    @property
    def has_period(self) -> bool:
        return self.period_start is not None and self.period_end is not None


def parse_cost_query(question: str, *, today: date | None = None) -> CostQueryFilters:
    q_full = question.strip().lower()
    parse_src = _last_user_utterance(question)
    q = parse_src.strip().lower()
    notes: list[str] = []
    env: str | None = None
    svc: str | None = None
    ref = today or date.today()

    if _mentions_prod(q_full) and _mentions_dev(q_full):
        env = None
        notes.append("comparing prod and dev (both environments)")
        notes.append("unlabeled projects appear as prod in export")
    elif _mentions_prod(q_full):
        env = "prod"
        notes.append("filtering environment=prod")
    elif _mentions_dev(q_full) and not _dev_mention_is_project_slug(question):
        env = "dev"
        notes.append("filtering environment=dev")

    svc_match = re.search(
        r"(compute engine|cloud storage|bigquery|cloud sql|artifact registry|networking|vertex ai|kubernetes engine|cloud run|cloud logging|logging)",
        q,
        re.I,
    )
    if svc_match:
        svc = svc_match.group(1).lower()
        notes.append(f"filtering service contains '{svc}'")

    ps, pe, pnotes = _parse_time_period(parse_src, q, ref)
    notes.extend(pnotes)
    if ps is None and pe is None and _mentions_till_now(question):
        scope = os.environ.get("BILLING_DEFAULT_TILL_NOW_SCOPE", "full_history").strip().lower()
        if scope in {"month_to_date", "mtd"}:
            ps = date(ref.year, ref.month, 1)
            pe = ref
            notes.append("defaulted to this month (month-to-date)")
        else:
            ps = _full_history_start(ref)
            pe = ref
            notes.append(f"defaulted to full history-to-date ({ps} to {pe})")

    br = _extract_billing_region(parse_src)
    if br:
        notes.append(f"filtering location.region={br}")

    bproj = _extract_gcp_project_id(parse_src)
    if bproj:
        notes.append(f"filtering project.id={bproj}")

    breakdown = bool(
        re.search(
            r"\b(breakdown|broken down|by\s+gcp\s+project|by\s+project|per\s+project|each\s+project)\b",
            q,
        )
    )
    wants_total = (
        bool(re.search(r"\b(total|sum|aggregate)\b", q))
        or (
            bool(re.search(r"\bhow\s+much\b", q))
            and bool(re.search(r"\b(spend|cost|pay|paid)\b", q))
        )
    ) and not breakdown
    wants_top = bool(re.search(r"\b(top|highest|largest|biggest|most\s+expensive)\b", q))

    hint = "; ".join(notes) if notes else "no explicit filters"
    return CostQueryFilters(
        env=env,
        svc=svc,
        billing_project_id=bproj,
        billing_region=br,
        period_start=ps,
        period_end=pe,
        wants_total=wants_total,
        wants_top=wants_top,
        hint=hint,
    )


def _preflight_window_with_dry_run(
    *,
    job_project: str,
    table_ref: str,
    start: date,
    end: date,
    date_column: str = "usage_start_time",
) -> tuple[bool, int]:
    max_bytes = int(os.environ.get("BILLING_LLM_MAX_BYTES_BILLED", "1000000000"))
    sql = (
        f"SELECT 1 FROM `{table_ref}` "
        f"WHERE DATE({date_column}) BETWEEN DATE('{start.isoformat()}') AND DATE('{end.isoformat()}') "
        "LIMIT 1"
    )
    client = bigquery.Client(project=job_project)
    job = client.query(
        sql,
        job_config=bigquery.QueryJobConfig(dry_run=True, use_query_cache=False),
    )
    est = int(job.total_bytes_processed or 0)
    return est <= max_bytes, est


def _mentions_till_now_phrase(text: str) -> bool:
    t = " ".join((text or "").lower().split())
    return bool(
        ("till now" in t)
        or ("until now" in t)
        or ("till date" in t)
        or ("to date" in t)
        or ("so far" in t)
    )


def _default_till_now_scope() -> str:
    v = os.environ.get("BILLING_DEFAULT_TILL_NOW_SCOPE", "full_history").strip().lower()
    if v in {"month_to_date", "mtd"}:
        return "month_to_date"
    return "full_history_to_date"


def compute_llm_date_window(
    f: CostQueryFilters,
    today: date,
    *,
    preflight_job_project: str | None = None,
    preflight_table_ref: str | None = None,
    date_column_for_preflight: str = "usage_start_time",
    original_question: str = "",
) -> tuple[date, date, str]:
    max_days = int(os.environ.get("BILLING_LLM_MAX_LOOKBACK_DAYS", "30"))
    allow_long = os.environ.get("BILLING_LLM_ALLOW_LONG_RANGE", "").lower() in ("1", "true", "yes")
    allow_explicit_calendar = os.environ.get(
        "BILLING_LLM_ALLOW_EXPLICIT_CALENDAR_WINDOW",
        "1",
    ).lower() in ("1", "true", "yes")
    notes: list[str] = []

    def is_full_calendar_month(start: date, end: date) -> bool:
        if start.year != end.year or start.month != end.month:
            return False
        last = calendar.monthrange(start.year, start.month)[1]
        return start.day == 1 and end.day == last

    latest_q = _last_user_utterance(original_question or "")
    till_now_in_latest = _mentions_till_now_phrase(latest_q)

    if max_days <= 0:
        if f.has_period and f.period_start is not None and f.period_end is not None:
            return (
                f.period_start,
                f.period_end,
                "No lookback day cap configured; using your requested window. Dry-run still enforces the byte cap.",
            )
        if till_now_in_latest and _default_till_now_scope() == "full_history_to_date":
            start = _full_history_start(today)
            return (
                start,
                today,
                f"No explicit period in your question — defaulting to full history-to-date ({start} to {today}).",
            )
        start = date(today.year, today.month, 1)
        return (
            start,
            today,
            "No explicit period in your question — defaulting to this month (month-to-date).",
        )

    if f.has_period and f.period_start is not None and f.period_end is not None:
        start, end = f.period_start, f.period_end
        span = (end - start).days + 1
        if span > max_days:
            if allow_long:
                notes.append(
                    f"Using your full requested window ({span} days) because BILLING_LLM_ALLOW_LONG_RANGE is enabled; dry-run still enforces the byte cap."
                )
            elif allow_explicit_calendar and is_full_calendar_month(start, end):
                notes.append(
                    f"Using full calendar-month window {start} through {end} ({span} days); dry-run still enforces the byte cap."
                )
            elif preflight_job_project and preflight_table_ref:
                try:
                    ok, est = _preflight_window_with_dry_run(
                        job_project=preflight_job_project,
                        table_ref=preflight_table_ref,
                        start=start,
                        end=end,
                        date_column=date_column_for_preflight,
                    )
                    if ok:
                        notes.append(
                            f"Using your full requested window ({span} days); preflight estimate {est} bytes is within cap. Dry-run still enforces the byte cap."
                        )
                    else:
                        start = end - timedelta(days=max_days - 1)
                        notes.append(
                            f"Requested window exceeded {max_days} days and preflight estimate {est} bytes exceeded cap; clamped to {start} through {end}. Narrow the range or set BILLING_LLM_MAX_LOOKBACK_DAYS=0."
                        )
                except Exception:
                    start = end - timedelta(days=max_days - 1)
                    notes.append(
                        f"Requested window exceeded {max_days} days; preflight unavailable, so clamped to {start} through {end}. Set BILLING_LLM_MAX_LOOKBACK_DAYS=0 to disable day-based clamping."
                    )
            else:
                start = end - timedelta(days=max_days - 1)
                notes.append(
                    f"Requested window exceeded {max_days} days; clamped to {start} through {end}. Narrow the range or set BILLING_LLM_ALLOW_LONG_RANGE=1."
                )
        return start, end, " ".join(notes) if notes else ""
    end = today
    if till_now_in_latest and _default_till_now_scope() == "full_history_to_date":
        start = _full_history_start(today)
        return (
            start,
            end,
            f"No explicit period in your question — defaulting to full history-to-date ({start} to {end}).",
        )
    start = today - timedelta(days=max_days - 1)
    return (
        start,
        end,
        f"No explicit period in your question — using the last {max_days} days through {end} (cost control).",
    )


def nl_to_sql(question: str) -> tuple[str, str]:
    """Minimal, safe SELECT-only SQL for cloud_costs. Returns (sql, reasoning_snippet)."""
    f = parse_cost_query(question)
    where: list[str] = []
    if f.env:
        where.append("environment = %s")
    if f.svc:
        where.append("LOWER(service_name) LIKE LOWER(%s)")
    if f.has_period:
        where.append("date BETWEEN %s::date AND %s::date")

    if f.wants_total:
        wh = " AND ".join(where) if where else "TRUE"
        return (
            "SELECT COALESCE(SUM(cost_usd), 0) AS total_usd FROM cloud_costs WHERE " + wh,
            f.hint,
        )

    wh = " AND ".join(where) if where else "TRUE"
    order = "cost_usd DESC, date DESC" if f.wants_top else "date DESC, id DESC"
    return (
        "SELECT id, date, service_name, environment, cost_usd FROM cloud_costs WHERE "
        f"{wh} ORDER BY {order} LIMIT 100",
        f.hint,
    )


def params_for_sql(sql: str, question: str) -> tuple:
    params: list = []
    f = parse_cost_query(question)
    if f.env and "environment = %s" in sql:
        params.append(f.env)
    if f.svc and "LIKE" in sql and "service_name" in sql:
        params.append(f"%{f.svc}%")
    if f.has_period and "BETWEEN" in sql:
        params.append(f.period_start.isoformat())
        params.append(f.period_end.isoformat())
    return tuple(params)


def run_query(sql: str, params: tuple) -> str:
    if not sql.strip().upper().startswith("SELECT"):
        raise ValueError("Only SELECT queries are allowed")
    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(sql, params)
            rows = cur.fetchall()
            colnames = [d[0] for d in cur.description] if cur.description else []
    lines = []
    for row in rows:
        lines.append(dict(zip(colnames, [str(c) for c in row])))
    return json.dumps(lines, indent=2)


def _bigquery_ready() -> bool:
    return bool(BQ_BILLING_DATASET and BQ_BILLING_TABLE)


def _normalize_env(raw: str | None) -> str:
    if not raw:
        return "prod"
    val = raw.strip().lower()
    if val in {"prod", "production", "prd"}:
        return "prod"
    if val in {"dev", "development"}:
        return "dev"
    return "prod"


def _bq_env_sql_fragment(env: str | None) -> str:
    labels = _project_labels_col()
    if not env:
        return ""
    if env == "prod":
        return """ AND (
          NOT EXISTS (
            SELECT 1 FROM UNNEST(IFNULL(""" + labels + """, [])) AS l
            WHERE LOWER(l.key) IN ('environment', 'env')
          )
          OR EXISTS (
            SELECT 1 FROM UNNEST(IFNULL(""" + labels + """, [])) AS l
            WHERE LOWER(l.key) IN ('environment', 'env')
              AND LOWER(l.value) IN ('prod', 'production', 'prd')
          )
        )"""
    if env == "dev":
        return """ AND EXISTS (
          SELECT 1 FROM UNNEST(IFNULL(""" + labels + """, [])) AS l
          WHERE LOWER(l.key) IN ('environment', 'env')
            AND LOWER(l.value) IN ('dev', 'development')
        )"""
    return ""


def _query_bigquery(question: str) -> str:
    f = parse_cost_query(question)
    table_project = BQ_BILLING_PROJECT or os.environ.get("GOOGLE_CLOUD_PROJECT", "")
    if not table_project:
        raise RuntimeError("Set BQ_BILLING_PROJECT or GOOGLE_CLOUD_PROJECT for BigQuery source.")
    table_ref = f"{table_project}.{BQ_BILLING_DATASET}.{BQ_BILLING_TABLE}"
    svc_col = _service_col()
    proj_col = _project_id_col()
    reg_col = _region_col()
    labels_col = _project_labels_col()

    filters: list[str] = []
    params: list[bigquery.ScalarQueryParameter] = []
    if f.svc:
        filters.append(
            f"STRPOS(LOWER(IFNULL({svc_col}, '')), LOWER(@service_needle)) > 0"
        )
        params.append(bigquery.ScalarQueryParameter("service_needle", "STRING", f.svc))
    if f.billing_region:
        filters.append(
            f"LOWER(TRIM(COALESCE({reg_col}, ''))) = LOWER(@billing_region)"
        )
        params.append(
            bigquery.ScalarQueryParameter("billing_region", "STRING", f.billing_region)
        )
    if f.billing_project_id:
        filters.append(f"{proj_col} = @billing_project_id")
        params.append(
            bigquery.ScalarQueryParameter("billing_project_id", "STRING", f.billing_project_id)
        )
    if f.has_period:
        filters.append("DATE(usage_start_time) BETWEEN @period_start AND @period_end")
        params.append(
            bigquery.ScalarQueryParameter("period_start", "DATE", f.period_start.isoformat())
        )
        params.append(bigquery.ScalarQueryParameter("period_end", "DATE", f.period_end.isoformat()))
    env_sql = _bq_env_sql_fragment(f.env)
    where_core = f"{' AND '.join(filters)}" if filters else "TRUE"
    where_sql = f"WHERE {where_core}{env_sql}"
    label_sql = """COALESCE(
        (
          SELECT ANY_VALUE(l.value)
          FROM UNNEST(IFNULL(""" + labels_col + """, [])) AS l
          WHERE LOWER(l.key) IN ('environment', 'env')
        ),
        'prod'
      ) AS raw_environment"""

    if f.wants_total:
        sql = f"SELECT COALESCE(SUM(cost), 0) AS total_inr FROM `{table_ref}` {where_sql}"
    elif f.wants_top:
        sql = f"""
        SELECT
          {svc_col} AS service_name,
          {label_sql},
          SUM(cost) AS cost_inr
        FROM `{table_ref}`
        {where_sql}
        GROUP BY 1, 2
        ORDER BY cost_inr DESC
        LIMIT 40
        """
    else:
        sql = f"""
        SELECT
          DATE(usage_start_time) AS usage_date,
          {svc_col} AS service_name,
          {label_sql},
          SUM(cost) AS cost_inr
        FROM `{table_ref}`
        {where_sql}
        GROUP BY 1, 2, 3
        ORDER BY usage_date DESC, service_name
        LIMIT 100
        """
    client = bigquery.Client(project=table_project)
    rows = list(
        client.query(sql, job_config=bigquery.QueryJobConfig(query_parameters=params)).result()
    )
    if f.wants_total:
        total = rows[0]["total_inr"] if rows else Decimal("0")
        return json.dumps([{"total_inr": str(total), "currency": "INR"}], indent=2)

    period_label = f"{f.period_start} to {f.period_end}" if f.has_period else ""
    normalized: list[dict[str, str]] = []
    for row in rows:
        row_env = _normalize_env(row["raw_environment"])
        if f.env and row_env != f.env:
            continue
        if f.wants_top:
            normalized.append(
                {
                    "date": period_label or "aggregated",
                    "service_name": str(row["service_name"]),
                    "environment": row_env,
                    "cost_inr": str(row["cost_inr"]),
                    "currency": "INR",
                }
            )
        else:
            usage_date = row["usage_date"]
            usage_date_val = usage_date.isoformat() if isinstance(usage_date, date) else str(usage_date)
            normalized.append(
                {
                    "date": usage_date_val,
                    "service_name": str(row["service_name"]),
                    "environment": row_env,
                    "cost_inr": str(row["cost_inr"]),
                    "currency": "INR",
                }
            )
    return json.dumps(normalized[:100], indent=2)


def _bq_table_ref() -> tuple[str, str]:
    table_project = BQ_BILLING_PROJECT or os.environ.get("GOOGLE_CLOUD_PROJECT", "")
    if not table_project:
        raise RuntimeError("Set BQ_BILLING_PROJECT or GOOGLE_CLOUD_PROJECT for BigQuery source.")
    return table_project, f"{table_project}.{BQ_BILLING_DATASET}.{BQ_BILLING_TABLE}"


def _bq_client() -> bigquery.Client:
    table_project, _ = _bq_table_ref()
    return bigquery.Client(project=table_project)


def _list_schema_fields(fields: list[Any], prefix: str = "") -> list[dict[str, str]]:
    rows: list[dict[str, str]] = []
    for field in fields:
        name = f"{prefix}{field.name}"
        rows.append(
            {
                "column_name": name,
                "type": str(field.field_type),
                "mode": str(field.mode or "NULLABLE"),
            }
        )
        nested = getattr(field, "fields", None) or []
        if nested:
            rows.extend(_list_schema_fields(list(nested), prefix=f"{name}."))
    return rows


def _bigquery_schema_rows_for_ref(table_ref: str) -> list[dict[str, str]]:
    client = _bq_client()
    table = client.get_table(table_ref)
    return _list_schema_fields(list(table.schema))


def _bigquery_schema_rows() -> list[dict[str, str]]:
    _, table_ref = _bq_table_ref()
    return _bigquery_schema_rows_for_ref(table_ref)


def _normalize_column_token(token: str) -> str:
    return token.strip().strip("`").strip('"').strip("'").strip()


def _extract_requested_column_name(question: str, schema_rows: list[dict[str, str]]) -> str | None:
    patterns = (
        r"(?i)\bcolumn\s+named\s+[`'\"]?([a-zA-Z_][\w.]*)[`'\"]?",
        r"(?i)\bcolumn\s+called\s+[`'\"]?([a-zA-Z_][\w.]*)[`'\"]?",
        r"(?i)\bin\s+the\s+[`'\"]?([a-zA-Z_][\w.]*)[`'\"]?\s+column\b",
        r"(?i)\bfor\s+[`'\"]?([a-zA-Z_][\w.]*)[`'\"]?\s+column\b",
        r"(?i)\bdoes\s+[`'\"]?([a-zA-Z_][\w.]*)[`'\"]?\s+exist\b",
        r"(?i)\bis\s+[`'\"]?([a-zA-Z_][\w.]*)[`'\"]?\s+(?:a\s+)?column\b",
    )
    for pattern in patterns:
        m = re.search(pattern, question)
        if m:
            return _normalize_column_token(m.group(1))
    valid = {row["column_name"].lower(): row["column_name"] for row in schema_rows}
    q_lower = question.lower()
    for key, original in sorted(valid.items(), key=lambda item: len(item[0]), reverse=True):
        if re.search(rf"(?<![\w.]){re.escape(key)}(?![\w.])", q_lower):
            return original
    return None


def _as_words(s: str) -> list[str]:
    out: list[str] = []
    for t in s.split():
        t = t.strip(".,?!;:\"'()[]`")
        if t:
            out.append(t.lower())
    return out


def _is_schema_list_query(question: str) -> bool:
    """BigQuery schema routing (no ``re``; keep regex out of this hot path)."""
    q = " ".join(question.lower().split())
    words = _as_words(q)
    return bool(
        ("what are all" in q and "column" in q and "names" in q)
        or ("list all" in q and "column" in q and "names" in q)
        or ("what columns" in q and ("exist" in q or "available" in q))
        or ("show" in q and "columns" in q)
        or ("schema" in words)
    )


def _is_column_existence_query(question: str) -> bool:
    q = " ".join(question.lower().split())
    if "which column" in q:
        return True
    if "does" in q and " exist" in q:
        return True
    if " is " in q and " column" in q:
        return True
    return False


def _is_distinct_value_query(question: str) -> bool:
    q = " ".join(question.lower().split())
    has_distinct = "unique" in q or "distinct" in q
    has_value = "value" in q or "values" in q
    has_field = any(x in q for x in ("column", "columns", "field", "fields", "attribute"))
    return has_distinct and has_value and has_field


def _schema_field_lookup(schema_rows: list[dict[str, str]]) -> dict[str, dict[str, str]]:
    return {row["column_name"].lower(): row for row in schema_rows}


def _is_supported_distinct_type(field_type: str) -> bool:
    return field_type.upper() in {
        "STRING",
        "INTEGER",
        "INT64",
        "FLOAT",
        "FLOAT64",
        "NUMERIC",
        "BIGNUMERIC",
        "BOOLEAN",
        "BOOL",
        "DATE",
        "TIMESTAMP",
    }


def _query_distinct_column_values(table_ref: str, column_name: str, field_type: str) -> str:
    sql = f"""
    SELECT DISTINCT `{column_name}` AS value
    FROM `{table_ref}`
    WHERE `{column_name}` IS NOT NULL
    ORDER BY value
    LIMIT {_SCHEMA_VALUE_LIMIT}
    """
    rows = list(_bq_client().query(sql).result())
    return json.dumps(
        [
            {
                "column_name": column_name,
                "type": field_type,
                "value": str(row["value"]),
            }
            for row in rows
        ],
        indent=2,
    )


def _query_bigquery_schema(question: str) -> str | None:
    if not _bigquery_ready():
        return None
    q = question.lower()
    if not (
        _is_schema_list_query(question)
        or _is_column_existence_query(question)
        or _is_distinct_value_query(question)
    ):
        return None
    try:
        schema_ref, table_label = _schema_table_choice(question)
    except RuntimeError:
        schema_ref, table_label = _bq_table_ref()[1], BQ_BILLING_TABLE
    schema_rows = _bigquery_schema_rows_for_ref(schema_ref)
    lookup = _schema_field_lookup(schema_rows)

    if _is_schema_list_query(question) and not _is_distinct_value_query(question):
        return json.dumps(schema_rows, indent=2)

    column_name = _extract_requested_column_name(question, schema_rows)
    if not column_name:
        return _clarification_payload(
            "Which column name do you want me to inspect?",
            [row["column_name"] for row in schema_rows[:10]],
            clarification_kind="schema_column",
            missing_slots=["column_name"],
        )
    field = lookup.get(column_name.lower())

    if _is_column_existence_query(question) and not _is_distinct_value_query(question):
        if field:
            return json.dumps(
                [
                    {
                        "column_name": field["column_name"],
                        "exists": "true",
                        "type": field["type"],
                        "mode": field["mode"],
                    }
                ],
                indent=2,
            )
        return _error_payload(
            "unknown_column",
            f"The column `{column_name}` does not exist in `{table_label}`; "
            f"cannot verify existence or list values for unknown columns.",
            "Ask for the full column list to inspect the live BigQuery schema.",
        )

    if _is_distinct_value_query(question):
        if not field:
            return _error_payload(
                "unknown_column",
                f"The column `{column_name}` does not exist in `{table_label}`; "
                f"cannot verify existence or list values for unknown columns.",
                "Ask for the full column list to inspect the live BigQuery schema.",
            )
        if "." in field["column_name"] or not _is_supported_distinct_type(field["type"]):
            return _error_payload(
                "unsupported_schema_query",
                f"Distinct-value listing is only supported for top-level scalar columns. `{field['column_name']}` is type {field['type']}.",
                "Try a top-level scalar column such as project_name, project_id, service_name, region, or currency.",
            )
        return _query_distinct_column_values(schema_ref, field["column_name"], field["type"])

    return None


def _error_payload(kind: str, detail: str, hint: str | None = None) -> str:
    payload: dict[str, str] = {"response_type": "error", "error": kind, "detail": detail}
    if hint:
        payload["hint"] = hint
    return json.dumps(payload, indent=2)


def _clarification_payload(
    question: str,
    options: list[str] | None = None,
    *,
    clarification_kind: str | None = None,
    missing_slots: list[str] | None = None,
    context: dict[str, Any] | None = None,
) -> str:
    payload: dict[str, object] = {
        "response_type": "clarification",
        "needs_clarification": True,
        "question": question.strip(),
    }
    if options:
        payload["options"] = [x.strip() for x in options if x and x.strip()]
    if clarification_kind:
        payload["clarification_kind"] = clarification_kind
    if missing_slots:
        payload["missing_slots"] = [x.strip() for x in missing_slots if x and x.strip()]
    if context:
        payload["context"] = context
    return json.dumps(payload, indent=2)


def _clarification_from_router_slots(routed: ResolvedCostContext) -> str | None:
    if not routed.needs_clarification:
        return None
    kind = (routed.clarification_kind or "").strip() or None
    missing = [str(x).strip() for x in (routed.missing_slots or []) if str(x).strip()]
    priority = (routed.clarification_priority or "").strip().lower() if routed.clarification_priority else ""
    if not kind:
        if priority == "top_n":
            kind = "top_n"
        elif priority == "compare_scope":
            kind = "compare_scope"
        elif priority == "compare_entities":
            kind = "compare_entities"
        elif priority == "column_name":
            kind = "schema_column"
        elif priority == "data_source":
            kind = "data_source"
        elif priority == "billing_project_id":
            kind = "billing_project_id"
        else:
            kind = "time_window"

    question = (routed.clarification_question or "").strip()
    options = [str(x).strip() for x in routed.clarification_options if str(x).strip()]

    if not question:
        if kind == "top_n":
            question = "How many results should I return for 'most expensive'?"
            options = ["Top 3", "Top 5", "Top 10"]
        elif kind == "compare_scope":
            question = "What two scopes should I compare?"
            options = ["prod vs dev", "project A vs project B", "service A vs service B"]
        elif kind == "compare_entities":
            question = "Which two services should I compare?"
            options = ["Cloud SQL vs Vertex AI", "BigQuery vs Cloud Storage", "Cloud Run vs Compute Engine"]
        elif kind == "schema_column":
            question = "Which column should I use?"
            options = []
        elif kind == "data_source":
            question = (
                "Should this query use the GCP billing export (INR) or the workflow / runtime view (USD, tokens)?"
            )
            options = ["GCP billing export", "Workflow / runtime view (tokens & traces)"]
        elif kind == "billing_project_id":
            question = "Which GCP project id should I filter on (billing export project.id)?"
            options = []
        else:
            question = "What time window should I use for this cost query?"
            options = ["Last 7 days", "This month (month-to-date)", "Full history to date"]

    return _clarification_payload(
        question,
        options or None,
        clarification_kind=kind,
        missing_slots=missing or None,
    )


def query_cost_data(question: str) -> tuple[str, str]:
    mode = SOURCE_MODE if SOURCE_MODE in {"auto", "bigquery", "postgres"} else "auto"
    f = parse_cost_query(question)
    hint = f.hint
    rewritten_question = question
    router_status = "router_skipped"
    table_project = BQ_BILLING_PROJECT or os.environ.get("GOOGLE_CLOUD_PROJECT", "")
    llm_on = os.environ.get("BILLING_AGENT_LLM_SQL", "1").lower() not in ("0", "false", "no")
    table_ref = (
        f"{table_project}.{BQ_BILLING_DATASET}.{BQ_BILLING_TABLE}"
        if table_project and _bigquery_ready()
        else ""
    )

    if mode in {"auto", "bigquery"} and _bigquery_ready() and table_project:
        if llm_on:
            if not llm_sql_usable():
                err = json.dumps(
                    {
                        "error": "llm_sql_unavailable",
                        "detail": "Install Vertex AI / Google AI dependencies and configure ADC or GOOGLE_AI_API_KEY.",
                    },
                    indent=2,
                )
                return err, f"{hint}; source=bigquery; currency=INR"
            bq_target = "gcp_billing"
            dual_source = _workflow_configured()
            work_question, forced_ds = _strip_forced_data_source_prefix(question)
            work_question, forced_bp = _strip_forced_billing_project_prefix(work_question)
            schema_digest = ""
            if os.environ.get("BILLING_SCHEMA_DIGEST", "").lower() in ("1", "true", "yes"):
                try:
                    schema_digest = build_dual_table_schema_digest()
                except Exception:
                    schema_digest = ""

            if os.environ.get("BILLING_CONTEXT_ROUTER_ENABLED", "1").lower() not in ("0", "false", "no"):
                if llm_context_router_usable():
                    try:
                        routed = resolve_cost_context(
                            work_question,
                            today=date.today(),
                            schema_digest=schema_digest or "",
                            dual_source_available=dual_source,
                        )
                        if _billing_legacy_regex_routing():
                            routed = _maybe_resolve_trace_query_without_clarification(
                                work_question, routed, date.today()
                            )
                            if _question_signals_usage_trace_table(work_question):
                                routed = replace(routed, bq_target="gcp_workflow")
                        if forced_ds:
                            ms = [
                                s
                                for s in (routed.missing_slots or [])
                                if str(s).strip().lower() != "data_source"
                            ]
                            nc = bool(ms)
                            upd: dict[str, Any] = {
                                "bq_target": forced_ds,
                                "bq_target_confidence": "high",
                                "missing_slots": ms,
                                "needs_clarification": nc,
                            }
                            if not nc:
                                upd.update(
                                    clarification_question=None,
                                    clarification_options=[],
                                    clarification_kind=None,
                                    clarification_priority=None,
                                )
                            routed = replace(routed, **upd)
                        if forced_bp:
                            rs = dict(routed.resolved_slots or {})
                            rs["billing_project_id"] = forced_bp
                            ms_bp = [
                                s
                                for s in (routed.missing_slots or [])
                                if str(s).strip().lower() != "billing_project_id"
                            ]
                            nc_bp = bool(ms_bp)
                            upd_bp: dict[str, Any] = {
                                "billing_project_id": forced_bp.strip().lower(),
                                "resolved_slots": rs,
                                "missing_slots": ms_bp,
                                "needs_clarification": nc_bp,
                            }
                            if not nc_bp:
                                upd_bp.update(
                                    clarification_question=None,
                                    clarification_options=[],
                                    clarification_kind=None,
                                    clarification_priority=None,
                                )
                            routed = replace(routed, **upd_bp)
                        clarification = _clarification_from_router_slots(routed)
                        if clarification is not None:
                            return (
                                clarification,
                                "router_requested_clarification; source=bigquery; currency=INR",
                            )
                        f = CostQueryFilters(
                            env=routed.env,
                            svc=routed.service,
                            billing_project_id=routed.billing_project_id,
                            billing_region=routed.billing_region,
                            period_start=routed.window_start,
                            period_end=routed.window_end,
                            wants_total=routed.wants_total,
                            wants_top=routed.wants_top,
                            hint=routed.hint,
                        )
                        rewritten_question = routed.rewritten_question or work_question
                        hint = f.hint
                        router_status = "router_ok"
                        bq_target = normalize_bq_target(routed.bq_target)
                    except Exception as e:
                        router_status = f"router_fallback({type(e).__name__})"
                        bq_target = (
                            _heuristic_bq_target(question) if _billing_legacy_regex_routing() else "gcp_billing"
                        )
                else:
                    bq_target = (
                        _heuristic_bq_target(question) if _billing_legacy_regex_routing() else "gcp_billing"
                    )
            else:
                bq_target = _heuristic_bq_target(question) if _billing_legacy_regex_routing() else "gcp_billing"

            if _billing_legacy_regex_routing() and _question_signals_usage_trace_table(question):
                bq_target = "gcp_workflow"

            bq_target = normalize_bq_target(bq_target)

            if bq_target == "gcp_workflow":
                if not _workflow_configured():
                    err = json.dumps(
                        {
                            "error": "workflow_view_not_configured",
                            "detail": (
                                "Set BQ_WORKFLOW_TABLE (optional BQ_WORKFLOW_PROJECT / BQ_WORKFLOW_DATASET; "
                                "or legacy BQ_COST_EVENTS_*; defaults to BQ_BILLING_*)."
                            ),
                        },
                        indent=2,
                    )
                    return err, f"{hint}; source=bigquery; bq_target=gcp_workflow"
                _, active_ref = _workflow_table_ref()
                date_col = "timestamp"
                currency_hint = "USD"
            else:
                active_ref = table_ref
                date_col = "usage_start_time"
                currency_hint = "INR"

            ws, we, wnote = compute_llm_date_window(
                f,
                date.today(),
                preflight_job_project=table_project,
                preflight_table_ref=active_ref,
                date_column_for_preflight=date_col,
                original_question=work_question,
            )
            extra = hint if hint and hint != "no explicit filters" else ""
            if router_status != "router_ok":
                extra = f"{extra}; {router_status}".strip("; ").strip()
            wnote_full = f"{wnote} Context hints: {extra}".strip() if extra else wnote
            cid = (
                _extract_usage_correlation_id(work_question)
                or _extract_usage_correlation_id(question)
                or _extract_usage_correlation_id(rewritten_question)
            )
            if (
                _billing_deterministic_trace_total()
                and bq_target == "gcp_workflow"
                and _workflow_configured()
                and cid
                and (_asks_scalar_total(work_question) or _asks_scalar_total(question))
            ):
                try:
                    total = _deterministic_trace_total_usd(
                        table_ref=active_ref,
                        job_project=table_project,
                        correlation_id=cid,
                        window_start=ws,
                        window_end=we,
                    )
                    row = {
                        "trace_id": cid,
                        "total_usd": round(total, 6),
                        "currency": "USD",
                        "window_start": ws.isoformat(),
                        "window_end": we.isoformat(),
                    }
                    body = json.dumps([row], ensure_ascii=False, indent=2)
                    sh = (
                        f"deterministic-usage-total; trace_id={cid}; window={ws}..{we}; "
                        f"bq_target=gcp_workflow"
                    )
                    return body, f"{hint}; {sh}; source=bigquery; bq_target={bq_target}; currency=USD"
                except Exception:
                    pass
            try:
                base_target = (
                    workflow_view_sql_target(active_ref)
                    if bq_target == "gcp_workflow"
                    else gcp_billing_sql_target(table_ref)
                )
                appendix = schema_digest.strip() if schema_digest else ""
                target = (
                    replace(base_target, schema_appendix=appendix) if appendix else base_target
                )
                body, sh = run_llm_cost_sql_query(
                    rewritten_question,
                    table_project,
                    ws,
                    we,
                    wnote_full,
                    target,
                )
                return body, f"{hint}; {sh}; source=bigquery; bq_target={bq_target}; currency={currency_hint}"
            except Exception as e:
                err = json.dumps(
                    {"error": "llm_sql_failed", "detail": str(e)},
                    indent=2,
                )
                return err, f"{hint}; llm-sql failed; source=bigquery; currency={currency_hint}"
        try:
            return (
                _query_bigquery(question),
                f"{hint}; source=bigquery; BILLING_AGENT_LLM_SQL=0; currency=INR",
            )
        except Exception as e:
            if mode == "bigquery":
                raise
            hint = f"{hint}; bigquery unavailable ({e}); fallback=postgres"

    sql, _ = nl_to_sql(question)
    params = params_for_sql(sql, question)
    return run_query(sql, params), f"{hint}; source=postgres (deprecated fallback)"


def query_costs(question: str) -> str:
    if SOURCE_MODE in {"auto", "bigquery"} and _bigquery_ready():
        try:
            schema_result = _query_bigquery_schema(question)
            if schema_result is not None:
                return schema_result
        except Exception as e:
            return _error_payload(
                "bigquery_schema_failed",
                str(e),
                "Check BQ_BILLING_* / optional BQ_WORKFLOW_* (or legacy BQ_COST_EVENTS_*) and IAM; table metadata must be readable.",
            )

    try:
        body, _ = query_cost_data(question)
        return body
    except Exception as e:
        return _error_payload(
            "query_failed",
            str(e),
            "Check BigQuery/Vertex configuration and billing table access.",
        )
