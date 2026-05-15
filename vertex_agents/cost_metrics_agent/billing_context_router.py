from __future__ import annotations

import json
import os
import re
import warnings
from dataclasses import dataclass
from datetime import date, timedelta
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

try:
    import vertexai
    from vertexai.generative_models import GenerationConfig, GenerativeModel

    _VERTEX_OK = True
except Exception:  # pragma: no cover
    _VERTEX_OK = False

try:
    with warnings.catch_warnings():
        warnings.simplefilter("ignore", FutureWarning)
        import google.generativeai as genai

    _GENAI_OK = True
except Exception:  # pragma: no cover
    _GENAI_OK = False


class BillingRoutePayload(BaseModel):
    model_config = ConfigDict(extra="forbid")

    rewritten_question: str = Field(..., min_length=1, max_length=2000)
    hint: str = Field(default="no explicit filters", max_length=2000)
    window_start: str | None = None
    window_end: str | None = None
    time_confident: bool = True
    env: str | None = Field(default=None, description="prod|dev|null")
    service: str | None = None
    billing_project_id: str | None = None
    billing_region: str | None = None
    wants_total: bool = False
    wants_top: bool = False
    intent_type: str | None = None
    required_slots: list[str] | None = None
    resolved_slots: dict[str, Any] | None = None
    clarification_priority: str | None = None
    needs_clarification: bool = False
    clarification_question: str | None = None
    clarification_options: list[str] | None = None
    clarification_kind: str | None = None
    missing_slots: list[str] | None = None
    time_scope: str | None = Field(
        default=None,
        description="explicit_window|month_to_date|full_history_to_date|unsure",
    )
    bq_target: str | None = Field(
        default=None,
        description="gcp_billing|gcp_workflow — which BigQuery view to query (legacy: agent_cost_events)",
    )
    bq_target_confidence: str | None = Field(
        default="high",
        description="high|medium|low — your confidence in bq_target",
    )
    usage_correlation_id: str | None = Field(
        default=None,
        description="For workflow view: exact trace_id string when the user gave one; null if N/A",
    )
    needs_data_source_clarification: bool = Field(
        default=False,
        description="True if invoice billing vs usage logs is ambiguous; ask user before querying",
    )


@dataclass(frozen=True)
class ResolvedCostContext:
    rewritten_question: str
    hint: str
    window_start: date | None
    window_end: date | None
    env: str | None
    service: str | None
    billing_project_id: str | None
    billing_region: str | None
    wants_total: bool
    wants_top: bool
    needs_clarification: bool
    clarification_question: str | None
    clarification_options: list[str]
    clarification_kind: str | None
    missing_slots: list[str]
    intent_type: str | None
    required_slots: list[str]
    resolved_slots: dict[str, Any]
    clarification_priority: str | None
    bq_target: str
    bq_target_confidence: str
    usage_correlation_id: str | None


def normalize_bq_target_confidence(raw: str | None) -> str:
    t = (raw or "high").strip().lower()
    if t in ("high", "medium", "low"):
        return t
    return "high"


def normalize_bq_target(raw: str | None) -> str:
    t = (raw or "").strip().lower()
    if t in (
        "gcp_workflow",
        "workflow",
        "workflow_view",
        "agent_cost_events",
        "cost_events",
        "llm_usage",
        "agent_usage",
        "events",
        "runtime",
        "runtime_view",
    ):
        return "gcp_workflow"
    return "gcp_billing"


ROUTER_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "rewritten_question": {"type": "string"},
        "hint": {"type": "string"},
        "window_start": {"type": "string"},
        "window_end": {"type": "string"},
        "time_confident": {"type": "boolean"},
        "env": {"type": "string"},
        "service": {"type": "string"},
        "billing_project_id": {"type": "string"},
        "billing_region": {"type": "string"},
        "wants_total": {"type": "boolean"},
        "wants_top": {"type": "boolean"},
        "intent_type": {"type": "string"},
        "required_slots": {"type": "array", "items": {"type": "string"}},
        "resolved_slots": {"type": "object"},
        "clarification_priority": {"type": "string"},
        "needs_clarification": {"type": "boolean"},
        "clarification_question": {"type": "string"},
        "clarification_options": {"type": "array", "items": {"type": "string"}},
        "clarification_kind": {"type": "string"},
        "missing_slots": {"type": "array", "items": {"type": "string"}},
        "time_scope": {"type": "string"},
        "bq_target": {
            "type": "string",
            "description": (
                "gcp_billing (invoice/SKUs/INR) or gcp_workflow (runtime view: trace_id, tokens, cost_usd USD). "
                "Legacy synonym agent_cost_events means gcp_workflow."
            ),
        },
        "bq_target_confidence": {"type": "string"},
        "usage_correlation_id": {"type": "string"},
        "needs_data_source_clarification": {"type": "boolean"},
    },
    "required": [
        "rewritten_question",
        "hint",
        "time_confident",
        "wants_total",
        "wants_top",
        "needs_clarification",
    ],
}


def _model_name() -> str:
    return (
        os.environ.get("BILLING_CONTEXT_ROUTER_MODEL")
        or os.environ.get("VERTEX_MODEL_ID")
        or "gemini-2.5-flash"
    ).strip()


def _provider() -> str:
    return os.environ.get("BILLING_LLM_PROVIDER", "auto").strip().lower()


def _google_ai_key() -> str | None:
    return (os.environ.get("GOOGLE_AI_API_KEY") or os.environ.get("GEMINI_API_KEY") or "").strip() or None


def llm_context_router_usable() -> bool:
    return _VERTEX_OK or (_GENAI_OK and bool(_google_ai_key()))


def _deployment_context_for_router() -> str:
    """Static deployment facts for the router (env-driven, not user-text regex)."""
    from .workflow_bq_env import workflow_raw_table_name, workflow_table_fqn

    bp = (os.environ.get("BQ_BILLING_PROJECT") or os.environ.get("GOOGLE_CLOUD_PROJECT", "")).strip()
    ds = os.environ.get("BQ_BILLING_DATASET", "").strip()
    bt = os.environ.get("BQ_BILLING_TABLE", "").strip()
    billing_ref = f"{bp}.{ds}.{bt}" if bp and ds and bt else "(billing table not fully configured)"
    lines = [
        "Deployment context (authoritative for this runtime):",
        f"- GCP billing view: `{billing_ref}`",
        "  Columns: billing_account_id, service_name, sku_description, usage_start_time, usage_end_time, "
        "invoice_month, project_id, project_name, region, country, cost, cost_at_list, currency, cost_type, "
        "usage_amount, usage_unit, usage_amount_in_pricing_units, pricing_unit, credits, resource_labels, project_labels.",
    ]
    wf_fqn = workflow_table_fqn(billing_project=bp, billing_dataset=ds) if bp and ds else None
    wf_name = workflow_raw_table_name()
    if wf_fqn:
        lines.append(f"- Workflow / runtime view: `{wf_fqn}`")
        lines.append(
            "  Columns: timestamp, trace_id, cost_usd, input_tokens, output_tokens — flat view (no jsonPayload)."
        )
    elif wf_name:
        lines.append(f"- Workflow view table name `{wf_name}` is set but project/dataset are incomplete.")
    else:
        lines.append("- Workflow / runtime view: not configured (set BQ_WORKFLOW_TABLE or legacy BQ_COST_EVENTS_TABLE).")
    default_pid = os.environ.get("BILLING_DEFAULT_PROJECT_ID", "").strip()
    if default_pid:
        lines.append(
            f"- Default GCP billing project id for filters: `{default_pid}`. "
            "When the user says our project / this project / the project (or equivalent) and does not name a different "
            f"GCP project id, set billing_project_id to `{default_pid}` in resolved_slots and do NOT add "
            "billing_project_id to required_slots or missing_slots."
        )
    else:
        lines.append(
            "- No BILLING_DEFAULT_PROJECT_ID is set. Only ask which GCP project id to use if the user explicitly "
            "scopes to a project you cannot infer from the conversation."
        )
    return "\n".join(lines) + "\n"


def _maybe_apply_default_billing_project(
    resolved_slots: dict[str, Any],
    required_slots: list[str],
    missing_slots: list[str],
) -> None:
    """Fill default billing project id when env is set and the model left the slot unresolved."""
    default_pid = os.environ.get("BILLING_DEFAULT_PROJECT_ID", "").strip()
    if not default_pid:
        return
    cur = str(resolved_slots.get("billing_project_id") or "").strip()
    if cur:
        return
    if "billing_project_id" not in missing_slots and "billing_project_id" not in required_slots:
        return
    resolved_slots["billing_project_id"] = default_pid
    while "billing_project_id" in required_slots:
        required_slots.remove("billing_project_id")
    while "billing_project_id" in missing_slots:
        missing_slots.remove("billing_project_id")


def _asks_for_ranked_top_n(text: str) -> bool:
    tl = " ".join(text.lower().split())
    return bool(
        re.search(r"\btop\s+\d+\b", tl)
        or "most expensive" in tl
        or "highest cost" in tl
        or "top spenders" in tl
    )


def _strip_top_n_if_full_service_list(
    message: str,
    rewritten: str,
    required_slots: list[str],
    missing_slots: list[str],
) -> None:
    """Do not require top_n when the user asked for a full per-service breakdown (not a ranked top-N)."""
    if _asks_for_ranked_top_n(message) or _asks_for_ranked_top_n(rewritten):
        return
    blob = f"{message} {rewritten}".lower().replace("-", " ")
    markers = (
        "all services",
        "every service",
        "each service",
        "service breakdown",
        "service wise",
        "servicewise",
        "list services",
        "list all services",
        "per service",
        "by service",
        "service wise cost",
        "service-wise",
    )
    if not any(m in blob for m in markers):
        return
    required_slots[:] = [s for s in required_slots if s != "top_n"]
    missing_slots[:] = [s for s in missing_slots if s != "top_n"]


def _router_prompt(message: str, today: date, *, schema_digest: str = "") -> str:
    digest_block = (
        f"\nLive BigQuery column digest (authoritative names; truncated):\n{schema_digest}\n"
        if schema_digest.strip()
        else ""
    )
    deploy = _deployment_context_for_router()
    return (
        "You are a billing and usage query router. Understand the latest user ask in context and output only JSON.\n"
        "Conversation may include lines prefixed USER:/ASSISTANT:. Use conversation context for follow-ups\n"
        '(e.g. "same as above but for 4 days").\n'
        f"Today is {today.isoformat()}.\n\n"
        f"{deploy}\n"
        f"{digest_block}\n"
        "Output fields:\n"
        "- rewritten_question: standalone query for the downstream BigQuery SQL assistant\n"
        "- hint: short semicolon-separated summary for UI/source hint\n"
        "- window_start/window_end: YYYY-MM-DD if explicit or inferred safely\n"
        "- time_confident: true if window is confidently inferred from user intent\n"
        "- env: prod/dev/null\n"
        "- service, billing_project_id, billing_region (or null)\n"
        "- wants_total / wants_top booleans\n"
        "- usage_correlation_id: when the user gives a trace / correlation id for the workflow view, set this to that "
        "exact string (it maps to column trace_id); else null.\n"
        "- bq_target: gcp_billing | gcp_workflow — choose from user intent and digest above. "
        "If the model outputs legacy label agent_cost_events, treat it as gcp_workflow.\n"
        "- bq_target_confidence: high | medium | low — how sure you are.\n"
        "- needs_data_source_clarification: true if the ask could reasonably be answered from either "
        "GCP invoice billing (SKUs, INR) OR the workflow/runtime view (tokens, trace_id, cost_usd USD); "
        "do not guess — ask the user. If confidence is not high, prefer clarification.\n\n"
        "MANDATORY routing (no exceptions): If the user's wording includes any of: runtime, workflow, trace, "
        "trace id / trace ID, token usage, input tokens, output tokens — you MUST set bq_target to gcp_workflow, "
        "set needs_data_source_clarification=false unless the question is genuinely about both invoice line items "
        "and workflow metrics in the same breath (then clarify). Do NOT send trace or token questions to gcp_billing.\n\n"
        "Intent types (pick the best fit):\n"
        "- cost_total: single aggregate or simple sum for a window.\n"
        "- top_n_ranking: user wants a bounded ranking (e.g. top 5 expensive services) — then require top_n.\n"
        "- discovery: user wants a full list / breakdown (e.g. all services, every service, service-wise breakdown "
        "without saying top N) — set wants_top=false, do NOT require top_n; prefer GROUP BY in SQL.\n"
        "- compare / schema / other as before.\n\n"
        "LLM-first slot contract:\n"
        "- Set required_slots to unresolved requirements among: time_window, top_n, compare_scope, "
        "compare_entities, column_name, data_source, billing_project_id (only if you truly cannot infer the GCP project id).\n"
        "- Set resolved_slots with known values (keys include: time_window, top_n, group_by, compare_scope, "
        "service_a, service_b, column_name, bq_target, billing_project_id).\n"
        "- Set clarification_priority to exactly one unresolved slot when clarification is needed.\n\n"
        "Clarification rules:\n"
        "- If required slots are missing, set needs_clarification=true.\n"
        "- Fill clarification_question with one concise question (never use internal slot names like billing_project_id in the question text).\n"
        "- Fill clarification_options with 2-5 concrete options when useful.\n"
        "- Fill clarification_kind with one of: time_window, compare_scope, compare_entities, top_n, "
        "schema_column, data_source, billing_project_id, other.\n"
        "- Fill missing_slots with required unresolved fields.\n"
        "- If question is executable, set needs_clarification=false and leave clarification_* empty.\n\n"
        "Priority rules:\n"
        "- For ranking asks ('most expensive', 'top 5 services') ask for top_n first if missing.\n"
        "- For 'all services' / 'service breakdown' / 'list every service' without a numeric top — use discovery, "
        "not top_n_ranking; wants_top=false.\n"
        "- For compare asks, ask compare_scope -> compare_entities -> time_window (one at a time).\n"
        "- For cost_total asks without an explicit time window, ask for time_window instead of guessing.\n"
        "- If needs_data_source_clarification is true, include data_source in missing_slots and set "
        "clarification_kind=data_source (or set clarification_priority=data_source).\n\n"
        "Data source semantics:\n"
        "- gcp_billing: Cloud Billing / invoice export style — services, SKUs, projects, regions, "
        "usage_start_time, cost in INR (see deployment column list).\n"
        "- gcp_workflow: Curated workflow/runtime view — timestamp, trace_id, cost_usd (USD), input_tokens, output_tokens. "
        "Use gcp_workflow for per-trace spend, token counts, agent runtime cost, or any question that names a trace id.\n"
        "- Use gcp_workflow when the user clearly wants usage-log style answers (tokens, trace, workflow, runtime).\n"
        "- Use gcp_billing when the user clearly wants GCP invoice / SKU / project / service billing.\n"
        "- When ambiguous (e.g. 'total cost for X' and X could be a trace id or a project label), set "
        "needs_data_source_clarification=true and needs_clarification=true.\n\n"
        "Time rules:\n"
        "- For 'this month till now' => first day of this month to today.\n"
        "- For 'March and April combined, until now' (or similar multi-month-to-date phrasing), "
        "set window_start to first day of the first named month and window_end=today.\n"
        "- For a named full month like 'March 2026', return full calendar bounds (e.g. 2026-03-01..2026-03-31).\n"
        "- For 'last N days' => inclusive range ending today.\n"
        "- For 'this year' => January 1st of current year through today.\n"
        "- For 'used till now' / 'until now' with no explicit month anchor, prefer full_history_to_date.\n"
        "- Set time_scope to one of: explicit_window, month_to_date, full_history_to_date, unsure.\n"
        "- If user is vague and cannot be resolved, set time_scope=unsure, time_confident=false and omit window fields.\n\n"
        f"Conversation:\n{message}"
    )


def _default_till_now_scope() -> str:
    v = os.environ.get("BILLING_DEFAULT_TILL_NOW_SCOPE", "full_history").strip().lower()
    if v in {"month_to_date", "mtd"}:
        return "month_to_date"
    return "full_history_to_date"


def _full_history_start(today: date) -> date:
    raw = os.environ.get("BILLING_FULL_HISTORY_START_DATE", "").strip()
    if raw:
        try:
            return date.fromisoformat(raw)
        except ValueError:
            pass
    return date(today.year, 1, 1)


def _looks_discovery_query(text: str) -> bool:
    t = text.lower()
    return (
        ("unique" in t or "distinct" in t or "list" in t)
        and ("service" in t or "services" in t)
    )


def _mentions_till_now(text: str) -> bool:
    t = " ".join(text.lower().split())
    return bool(
        ("till now" in t)
        or ("until now" in t)
        or ("till date" in t)
        or ("to date" in t)
        or ("so far" in t)
    )


def _parse_json(raw: str) -> BillingRoutePayload:
    text = (raw or "").strip()
    if not text:
        raise RuntimeError("Router returned empty response")
    data = json.loads(text)
    return BillingRoutePayload.model_validate(data)


def _slot_list(value: list[str] | None) -> list[str]:
    return [str(v).strip() for v in (value or []) if str(v).strip()]


def _normalized_intent(payload: BillingRoutePayload, message: str) -> str:
    intent = (payload.intent_type or "").strip().lower()
    if intent:
        return intent
    text = (payload.rewritten_question or message).lower()
    if payload.wants_top:
        return "top_n_ranking"
    if "compare" in text or " vs " in text:
        return "compare"
    if payload.wants_total:
        return "cost_total"
    return "other"


def _missing_required_slots(
    required_slots: list[str],
    resolved_slots: dict[str, Any],
    *,
    window_resolved: bool,
) -> list[str]:
    missing: list[str] = []
    for slot in required_slots:
        if slot == "time_window":
            if not window_resolved and not str(resolved_slots.get("time_window") or "").strip():
                missing.append(slot)
            continue
        if slot == "data_source":
            bt = str(resolved_slots.get("bq_target") or "").strip().lower()
            if bt not in {"gcp_billing", "gcp_workflow"}:
                missing.append(slot)
            continue
        if slot == "compare_entities":
            service_a = str(resolved_slots.get("service_a") or "").strip()
            service_b = str(resolved_slots.get("service_b") or "").strip()
            if not service_a or not service_b:
                missing.append(slot)
            continue
        value = resolved_slots.get(slot)
        if isinstance(value, str):
            if not value.strip():
                missing.append(slot)
        elif value is None:
            missing.append(slot)
    return missing


def _sanitized_str(s: str | None) -> str | None:
    t = (s or "").strip()
    if not t or t.lower() in ("null", "none", "undefined"):
        return None
    return t


def _apply_deterministic_slot_overrides(
    message: str,
    today: date,
    scope: str,
    *,
    window_from_payload: bool,
    ws: date | None,
    we: date | None,
    resolved_slots: dict[str, Any],
    required_slots: list[str],
    missing_slots: list[str],
) -> tuple[str, bool, date | None, date | None, dict[str, Any], list[str], list[str]]:
    """Remove false-positive missing slots (explicit top-N, compare pair, time window)."""
    msg = message
    m_lower = " ".join(msg.lower().split())
    rslots: dict[str, Any] = dict(resolved_slots)
    rreq = list(required_slots)
    miss = list(missing_slots)
    scope_out = scope
    wf, w0, w1 = window_from_payload, ws, we

    m_top = re.search(r"\btop\s*(\d+)\b", msg, re.IGNORECASE)
    if m_top:
        rslots["top_n"] = m_top.group(1)
        miss = [s for s in miss if s != "top_n"]
        rreq = [s for s in rreq if s != "top_n"]

    m_days = re.search(r"\blast\s+(\d+)\s+days?\b", m_lower)
    if m_days:
        n = int(m_days.group(1))
        w1 = today
        w0 = today - timedelta(days=n - 1)
        wf = True
        rslots["time_window"] = f"last {n} days"
        miss = [s for s in miss if s != "time_window"]
        rreq = [s for s in rreq if s != "time_window"]
        if scope_out in ("", "unsure"):
            scope_out = "explicit_window"

    if re.search(r"\bthis month\b", m_lower) and not wf:
        w0 = date(today.year, today.month, 1)
        w1 = today
        wf = True
        rslots["time_window"] = "this month (month-to-date)"
        miss = [s for s in miss if s != "time_window"]
        rreq = [s for s in rreq if s != "time_window"]
        if scope_out in ("", "unsure"):
            scope_out = "month_to_date"

    gcp_a = re.search(r"cloud\s*sql", m_lower) is not None
    gcp_b = re.search(r"vertex(?:\s*ai)?", m_lower) is not None
    if gcp_a and gcp_b:
        rslots.setdefault("service_a", "Cloud SQL")
        rslots.setdefault("service_b", "Vertex AI")
        for sname in ("compare_scope", "compare_entities"):
            miss = [s for s in miss if s != sname]
            rreq = [s for s in rreq if s != sname]

    if rslots.get("service_a") and rslots.get("service_b"):
        for sname in ("compare_scope", "compare_entities"):
            miss = [s for s in miss if s != sname]
            rreq = [s for s in rreq if s != sname]

    return (
        scope_out,
        wf,
        w0,
        w1,
        rslots,
        rreq,
        miss,
    )


def _clarification_for_slot(slot: str) -> tuple[str, list[str], str]:
    if slot == "top_n":
        return (
            "How many results should I return for 'most expensive'?",
            ["Top 3", "Top 5", "Top 10"],
            "top_n",
        )
    if slot == "compare_scope":
        return (
            "What two scopes should I compare?",
            ["prod vs dev", "project A vs project B", "service A vs service B"],
            "compare_scope",
        )
    if slot == "compare_entities":
        return (
            "Which two services should I compare?",
            ["Cloud SQL vs Vertex AI", "BigQuery vs Cloud Storage", "Cloud Run vs Compute Engine"],
            "compare_entities",
        )
    if slot == "column_name":
        return (
            "Which column should I use?",
            [],
            "schema_column",
        )
    if slot == "data_source":
        return (
            "Should I query GCP invoice billing (INR) or the workflow / runtime view (USD, tokens, trace_id)?",
            ["GCP billing export", "Workflow / runtime view (tokens & traces)"],
            "data_source",
        )
    if slot == "billing_project_id":
        return (
            "Which GCP project id should I filter on for billing (for example the value in project.id)?",
            [],
            "billing_project_id",
        )
    return (
        "What time window should I use for this cost query?",
        ["Last 7 days", "This month (month-to-date)", "Full history to date"],
        "time_window",
    )


def _invoke_vertex(prompt: str) -> BillingRoutePayload:
    if not _VERTEX_OK:
        raise RuntimeError("vertex SDK unavailable")
    project = (os.environ.get("GOOGLE_CLOUD_PROJECT") or os.environ.get("BQ_BILLING_PROJECT", "")).strip()
    if not project:
        raise RuntimeError("GOOGLE_CLOUD_PROJECT required for Vertex router")
    location = os.environ.get("GOOGLE_CLOUD_LOCATION", "us-central1").strip()
    vertexai.init(project=project, location=location)
    model = GenerativeModel(_model_name())
    cfg = GenerationConfig(
        temperature=0.1,
        max_output_tokens=2048,
        response_mime_type="application/json",
        response_schema=ROUTER_SCHEMA,
    )
    r = model.generate_content(prompt, generation_config=cfg)
    return _parse_json(r.text or "")


def _invoke_google_ai(prompt: str) -> BillingRoutePayload:
    if not _GENAI_OK:
        raise RuntimeError("google-generativeai unavailable")
    key = _google_ai_key()
    if not key:
        raise RuntimeError("GOOGLE_AI_API_KEY or GEMINI_API_KEY required")
    genai.configure(api_key=key)
    model = genai.GenerativeModel(_model_name())
    cfg = genai.GenerationConfig(
        temperature=0.1,
        max_output_tokens=2048,
        response_mime_type="application/json",
        response_schema=ROUTER_SCHEMA,
    )
    r = model.generate_content(prompt, generation_config=cfg)
    text = (getattr(r, "text", None) or "").strip()
    if not text and r.candidates:
        parts = r.candidates[0].content.parts
        text = "".join(getattr(p, "text", "") for p in parts).strip()
    return _parse_json(text)


def _invoke_router(prompt: str) -> BillingRoutePayload:
    provider = _provider()
    key = _google_ai_key()
    if provider == "vertex":
        return _invoke_vertex(prompt)
    if provider == "google_ai":
        return _invoke_google_ai(prompt)
    v_err: BaseException | None = None
    if _VERTEX_OK:
        try:
            return _invoke_vertex(prompt)
        except Exception as e:  # noqa: BLE001
            v_err = e
            if not key:
                raise
    if key and _GENAI_OK:
        return _invoke_google_ai(prompt)
    if v_err:
        raise RuntimeError("Vertex context router failed and no Google AI fallback key configured") from v_err
    raise RuntimeError("No router backend available")


def resolve_cost_context(
    message: str,
    *,
    today: date,
    schema_digest: str = "",
    dual_source_available: bool = True,
) -> ResolvedCostContext:
    payload = _invoke_router(_router_prompt(message, today, schema_digest=schema_digest))
    scope = (payload.time_scope or "").strip().lower()
    raw_hint = payload.hint.strip() or "no explicit filters"
    text = payload.rewritten_question or message
    ws: date | None = None
    we: date | None = None
    window_from_payload = False
    if payload.window_start and payload.window_end:
        try:
            ws = date.fromisoformat(payload.window_start.strip())
            we = date.fromisoformat(payload.window_end.strip())
            window_from_payload = ws is not None and we is not None
        except ValueError:
            ws = None
            we = None
    intent = _normalized_intent(payload, message)
    required_slots = _slot_list(payload.required_slots)
    resolved_slots = dict(payload.resolved_slots if isinstance(payload.resolved_slots, dict) else {})
    if "bq_target" in resolved_slots and str(resolved_slots.get("bq_target") or "").strip():
        resolved_slots["bq_target"] = normalize_bq_target(str(resolved_slots.get("bq_target")))
    pid_top = _sanitized_str(payload.billing_project_id)
    if pid_top:
        resolved_slots.setdefault("billing_project_id", pid_top)
    if not required_slots:
        if intent == "top_n_ranking":
            required_slots = ["top_n", "time_window"]
        elif intent == "compare":
            required_slots = ["compare_scope", "compare_entities", "time_window"]
        elif intent == "cost_total":
            required_slots = ["time_window"]
        elif intent == "discovery":
            required_slots = ["time_window"]
    has_time_scope = scope in {"month_to_date", "mtd", "full_history_to_date"}
    resolved_time_window = str(resolved_slots.get("time_window") or "").strip()
    window_resolved = window_from_payload or has_time_scope or bool(resolved_time_window)
    missing_slots = _slot_list(payload.missing_slots)
    if not missing_slots:
        missing_slots = _missing_required_slots(required_slots, resolved_slots, window_resolved=window_resolved)
    _maybe_apply_default_billing_project(resolved_slots, required_slots, missing_slots)
    missing_slots = _missing_required_slots(required_slots, resolved_slots, window_resolved=window_resolved)
    (
        scope,
        window_from_payload,
        ws,
        we,
        resolved_slots,
        required_slots,
        missing_slots,
    ) = _apply_deterministic_slot_overrides(
        message,
        today,
        scope,
        window_from_payload=window_from_payload,
        ws=ws,
        we=we,
        resolved_slots=resolved_slots,
        required_slots=required_slots,
        missing_slots=missing_slots,
    )
    has_time_scope = scope in {"month_to_date", "mtd", "full_history_to_date"}
    resolved_time_window = str(resolved_slots.get("time_window") or "").strip()
    window_resolved = window_from_payload or has_time_scope or bool(resolved_time_window)
    missing_slots = _missing_required_slots(required_slots, resolved_slots, window_resolved=window_resolved)
    _maybe_apply_default_billing_project(resolved_slots, required_slots, missing_slots)
    _strip_top_n_if_full_service_list(message, text, required_slots, missing_slots)
    _maybe_apply_default_billing_project(resolved_slots, required_slots, missing_slots)
    missing_slots = _missing_required_slots(required_slots, resolved_slots, window_resolved=window_resolved)
    if dual_source_available and bool(payload.needs_data_source_clarification):
        if "data_source" not in missing_slots:
            missing_slots = ["data_source", *missing_slots]
    elif dual_source_available and normalize_bq_target_confidence(payload.bq_target_confidence) in (
        "medium",
        "low",
    ):
        if "data_source" not in missing_slots:
            missing_slots = ["data_source", *missing_slots]
    needs_clarification = bool(missing_slots)
    clarification_priority = (payload.clarification_priority or "").strip().lower() or None
    if clarification_priority in ("null", "none", "undefined", ""):
        clarification_priority = None
    if needs_clarification and not clarification_priority:
        clarification_priority = missing_slots[0] if missing_slots else None
    clarification_question = _sanitized_str((payload.clarification_question or "").strip() or None)
    clarification_options = [str(x).strip() for x in (payload.clarification_options or []) if str(x).strip()]
    kind_raw = _sanitized_str((payload.clarification_kind or "").strip() or None)
    clarification_kind = kind_raw
    if needs_clarification and clarification_priority and not clarification_question:
        clarification_question, clarification_options, clarification_kind = _clarification_for_slot(clarification_priority)
    if needs_clarification and clarification_priority and not clarification_kind:
        _, _, clarification_kind = _clarification_for_slot(clarification_priority)
    if not needs_clarification:
        if ws is not None and we is not None:
            hint = raw_hint
        elif scope in {"month_to_date", "mtd"} or "this month" in text.lower():
            ws = date(today.year, today.month, 1)
            we = today
            hint = raw_hint + "; defaulted to this month (month-to-date)"
        elif scope == "full_history_to_date" or _mentions_till_now(text) or _looks_discovery_query(text):
            ws = _full_history_start(today)
            we = today
            hint = raw_hint + f"; defaulted to full history-to-date ({ws} to {we})"
        else:
            if _default_till_now_scope() == "month_to_date":
                ws = date(today.year, today.month, 1)
                we = today
                hint = raw_hint + "; defaulted to this month (month-to-date)"
            else:
                ws = _full_history_start(today)
                we = today
                hint = raw_hint + f"; defaulted to full history-to-date ({ws} to {we})"
    else:
        hint = raw_hint

    env = payload.env.strip().lower() if payload.env else None
    if env not in {"prod", "dev"}:
        env = None
    bq_t = normalize_bq_target(payload.bq_target)
    rw = payload.rewritten_question.strip()
    ucid = _sanitized_str(payload.usage_correlation_id)
    if ucid and bq_t == "gcp_workflow" and ucid.lower() not in rw.lower():
        rw = f"{rw}\n(Filter workflow view: trace_id equals the correlation id {ucid!r}.)"
    merged_bpid = _sanitized_str(str(resolved_slots.get("billing_project_id") or payload.billing_project_id or ""))
    wants_top_out = (bool(payload.wants_top) or bool(str(resolved_slots.get("top_n") or "").strip())) and (
        "top_n" in required_slots or "top_n" in missing_slots or bool(str(resolved_slots.get("top_n") or "").strip())
    )
    return ResolvedCostContext(
        rewritten_question=rw,
        hint=hint,
        window_start=ws,
        window_end=we,
        env=env,
        service=(payload.service or "").strip().lower() or None,
        billing_project_id=merged_bpid.lower() if merged_bpid else None,
        billing_region=(payload.billing_region or "").strip().lower() or None,
        wants_total=bool(payload.wants_total),
        wants_top=wants_top_out,
        needs_clarification=needs_clarification,
        clarification_question=clarification_question,
        clarification_options=clarification_options,
        clarification_kind=clarification_kind,
        missing_slots=missing_slots,
        intent_type=intent,
        required_slots=required_slots,
        resolved_slots=resolved_slots,
        clarification_priority=clarification_priority,
        bq_target=bq_t,
        bq_target_confidence=normalize_bq_target_confidence(payload.bq_target_confidence),
        usage_correlation_id=ucid,
    )
