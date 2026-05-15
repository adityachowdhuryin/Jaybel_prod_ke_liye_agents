"""
Guarded LLM-generated BigQuery SQL for billing analytics.

Backends (see BILLING_LLM_PROVIDER):
- Vertex AI (ADC): needs roles/aiplatform.user (predict on Gemini).
- Google AI API: set GOOGLE_AI_API_KEY or GEMINI_API_KEY (no Vertex IAM).

SQL is returned as structured JSON (response_schema) — no markdown / fence parsing.
"""
from __future__ import annotations

import json
import os
import re
import warnings
from dataclasses import dataclass
from datetime import date
from typing import Any

from google.cloud import bigquery
from pydantic import BaseModel, ConfigDict, Field

from .billing_schema import is_clean_view_mode, llm_schema_description
from .workflow_view_schema import llm_workflow_view_schema_description

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

MAX_BYTES_DEFAULT = 1_000_000_000
MAX_RESULT_ROWS = 512


@dataclass(frozen=True)
class BqSqlTarget:
    """Single-table LLM SQL generation profile (billing view or workflow runtime view)."""

    table_ref: str
    date_column: str
    schema_text: str
    role_line: str
    amount_line: str
    schema_mode_note: str
    schema_appendix: str = ""


def gcp_billing_sql_target(table_ref: str) -> BqSqlTarget:
    schema = llm_schema_description().format(table_ref=table_ref)
    note = (
        "You are querying the clean billing view. Use clean columns (e.g. service_name, project_id, region, project_labels) "
        "and do NOT use nested raw-export fields like service.description or project.id."
        if is_clean_view_mode()
        else "You are querying the raw export table with nested fields (e.g. service.description, project.id, location.region)."
    )
    return BqSqlTarget(
        table_ref=table_ref,
        date_column="usage_start_time",
        schema_text=schema,
        role_line="You are a BigQuery analyst for GCP billing exports.",
        amount_line=(
            "6. Amounts: SUM(cost); alias totals as total_inr or similar. Mention INR in column names where helpful."
        ),
        schema_mode_note=note,
        schema_appendix="",
    )


def workflow_view_sql_target(table_ref: str) -> BqSqlTarget:
    schema = llm_workflow_view_schema_description(table_ref)
    return BqSqlTarget(
        table_ref=table_ref,
        date_column="timestamp",
        schema_text=schema,
        role_line="You are a BigQuery analyst for agent workflow/runtime usage (USD on cost_usd, tokens, trace_id).",
        amount_line=(
            "6. Amounts: SUM(IFNULL(cost_usd, 0)); alias totals as total_usd or similar. All monetary amounts are USD."
        ),
        schema_mode_note=(
            "You are querying the curated workflow/runtime view, not GCP invoice billing. "
            "Use top-level columns only (timestamp, trace_id, cost_usd, input_tokens, output_tokens)."
        ),
        schema_appendix="",
    )


def agent_cost_events_sql_target(table_ref: str) -> BqSqlTarget:
    """Deprecated alias for workflow_view_sql_target (legacy name)."""
    return workflow_view_sql_target(table_ref)


class BillingSqlGeneration(BaseModel):
    model_config = ConfigDict(extra="forbid")

    sql: str = Field(
        ...,
        min_length=1,
        description="Single BigQuery WITH...SELECT or SELECT; raw SQL only, no markdown.",
    )
    rationale: str | None = Field(
        default=None,
        max_length=4000,
        description="Optional short note for logging; omit if unnecessary.",
    )


BILLING_SQL_RESPONSE_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "sql": {
            "type": "string",
            "description": (
                "One BigQuery statement only: WITH ... SELECT or plain SELECT. "
                "RAW SQL ONLY — no markdown fences, no prose, no backticks around the statement. "
                "Must satisfy the date window and table rules from the user prompt."
            ),
        },
        "rationale": {
            "type": "string",
            "description": "Optional brief note for operators. Omit if not needed.",
        },
    },
    "required": ["sql"],
}


_FORBIDDEN = re.compile(
    r"\b(INSERT|UPDATE|DELETE|MERGE|CREATE|DROP|ALTER|TRUNCATE|GRANT|REVOKE|CALL|EXECUTE)\b",
    re.I,
)


def vertex_available() -> bool:
    return _VERTEX_OK


def google_ai_api_key() -> str | None:
    return (os.environ.get("GOOGLE_AI_API_KEY") or os.environ.get("GEMINI_API_KEY") or "").strip() or None


def google_ai_configured() -> bool:
    return _GENAI_OK and bool(google_ai_api_key())


def llm_sql_usable() -> bool:
    return _VERTEX_OK or google_ai_configured()


def _is_vertex_permission_error(exc: BaseException) -> bool:
    s = str(exc).lower()
    return "403" in s or "permission_denied" in s or "iam_permission_denied" in s.replace(" ", "_")


def _build_sql_prompt(
    target: BqSqlTarget,
    window_start: date,
    window_end: date,
    window_note: str,
    question: str,
) -> str:
    ws, we = window_start.isoformat(), window_end.isoformat()
    dc = target.date_column
    appendix = (target.schema_appendix or "").strip()
    appendix_block = f"\nAdditional live schema (truncated):\n{appendix}\n" if appendix else ""
    return f"""{target.role_line}

{target.schema_text}
{target.schema_mode_note}
{appendix_block}
Hard requirements:
1. A single statement only: WITH ... SELECT ... or plain SELECT. No DDL/DML, no multi-statement.
2. FROM / JOIN must only reference `{target.table_ref}` (UNNEST of its columns is allowed).
3. WHERE must include exactly this predicate (you may AND more conditions after it):
   DATE({dc}) BETWEEN DATE('{ws}') AND DATE('{we}')
4. Use explicit DATE('YYYY-MM-DD') literals for those bounds (do not use parameters).
5. Prefer aggregates; for wide scans add LIMIT {MAX_RESULT_ROWS} or less.
{target.amount_line}
7. Output: you MUST fill the response JSON fields exactly per the API schema: put the full SQL in the `sql` string only (no markdown, no ``` fences). Optional short `rationale` for operators only.

If the user refers to "the same question as above" or "as before", infer the same analytical intent (e.g. top SKUs, breakdown by region) from the conversation and apply any new filters (project id, dates) they specify.

Window context: {window_note}

User question:
{question}
"""


def _parse_billing_sql_json_payload(raw: str) -> BillingSqlGeneration:
    text = (raw or "").strip()
    if not text:
        raise RuntimeError("Model returned empty response.")
    try:
        data = json.loads(text)
    except json.JSONDecodeError as e:
        raise RuntimeError(f"Model returned invalid JSON: {e}") from e
    return BillingSqlGeneration.model_validate(data)


def _invoke_vertex(prompt: str) -> BillingSqlGeneration:
    if not _VERTEX_OK:
        raise RuntimeError("google-cloud-aiplatform / vertexai not installed.")
    project = os.environ.get("GOOGLE_CLOUD_PROJECT") or os.environ.get("BQ_BILLING_PROJECT", "")
    location = os.environ.get("GOOGLE_CLOUD_LOCATION", "us-central1")
    model_id = (
        os.environ.get("BILLING_LLM_MODEL")
        or os.environ.get("VERTEX_MODEL_ID")
        or "gemini-2.5-flash"
    ).strip() or "gemini-2.5-flash"
    if not project:
        raise RuntimeError("GOOGLE_CLOUD_PROJECT (or BQ_BILLING_PROJECT) must be set for Vertex.")
    vertexai.init(project=project, location=location)
    model = GenerativeModel(model_id)
    gen_cfg = GenerationConfig(
        temperature=0.1,
        max_output_tokens=4096,
        response_mime_type="application/json",
        response_schema=BILLING_SQL_RESPONSE_SCHEMA,
    )
    r = model.generate_content(prompt, generation_config=gen_cfg)
    return _parse_billing_sql_json_payload(r.text or "")


def _invoke_google_ai(prompt: str) -> BillingSqlGeneration:
    if not _GENAI_OK:
        raise RuntimeError("google-generativeai is not installed.")
    key = google_ai_api_key()
    if not key:
        raise RuntimeError("Set GOOGLE_AI_API_KEY or GEMINI_API_KEY for Google AI fallback.")
    mid = os.environ.get("BILLING_LLM_GOOGLE_AI_MODEL", "gemini-2.5-flash")
    genai.configure(api_key=key)
    model = genai.GenerativeModel(mid)
    gen_cfg = genai.GenerationConfig(
        temperature=0.1,
        max_output_tokens=4096,
        response_mime_type="application/json",
        response_schema=BILLING_SQL_RESPONSE_SCHEMA,
    )
    r = model.generate_content(prompt, generation_config=gen_cfg)
    text = (getattr(r, "text", None) or "").strip()
    if not text and r.candidates:
        parts = r.candidates[0].content.parts
        text = "".join(getattr(p, "text", "") for p in parts).strip()
    return _parse_billing_sql_json_payload(text)


def _generate_billing_sql_generation(
    question: str,
    target: BqSqlTarget,
    window_start: date,
    window_end: date,
    window_note: str,
) -> BillingSqlGeneration:
    prompt = _build_sql_prompt(target, window_start, window_end, window_note, question)
    provider = os.environ.get("BILLING_LLM_PROVIDER", "auto").strip().lower()
    key = google_ai_api_key()

    if provider == "google_ai":
        return _invoke_google_ai(prompt)
    if provider == "vertex":
        return _invoke_vertex(prompt)

    v_err: BaseException | None = None
    if _VERTEX_OK:
        try:
            return _invoke_vertex(prompt)
        except Exception as e:  # noqa: BLE001
            v_err = e
            if not _is_vertex_permission_error(e) or not key:
                raise
    if key and _GENAI_OK:
        return _invoke_google_ai(prompt)
    if v_err:
        raise RuntimeError(
            "Vertex AI denied access (aiplatform.endpoints.predict). Grant roles/aiplatform.user, "
            "or set GOOGLE_AI_API_KEY / GEMINI_API_KEY for fallback."
        ) from v_err
    raise RuntimeError(
        "No LLM backend available: install google-cloud-aiplatform for Vertex, "
        "or set GOOGLE_AI_API_KEY with google-generativeai installed."
    )


def _strip_sql_comments(sql: str) -> str:
    s = re.sub(r"/\*.*?\*/", " ", sql, flags=re.S)
    s = re.sub(r"--[^\n]*", " ", s)
    return s


def _first_statement(sql: str) -> str:
    body = sql.strip()
    parts = [p.strip() for p in _strip_sql_comments(body).split(";") if p.strip()]
    if not parts:
        return ""
    return parts[0].strip()


def _normalize_table_reference(sql: str, table_ref: str) -> str:
    tick = f"`{table_ref}`"
    if tick in sql:
        return sql
    bits = table_ref.split(".")
    if len(bits) == 3:
        dotted_tick = f"`{bits[0]}`.`{bits[1]}`.`{bits[2]}`"
        if dotted_tick in sql:
            return sql.replace(dotted_tick, tick)
    if table_ref in sql:
        return sql.replace(table_ref, tick)
    return sql


def _validate_llm_sql(sql_raw: str, target: BqSqlTarget, window_start: date, window_end: date) -> str:
    sql = _first_statement(sql_raw)
    if not sql:
        raise ValueError("Model returned empty SQL.")
    if _FORBIDDEN.search(sql):
        raise ValueError("Only SELECT (or WITH ... SELECT) queries are allowed.")
    cleaned = _strip_sql_comments(sql)
    if not re.match(r"^\s*(WITH\b[\s\S]*?\bSELECT\b|SELECT\b)", cleaned, re.I):
        raise ValueError("Only SELECT (or WITH ... SELECT) queries are allowed.")

    table_ref = target.table_ref
    sql = _normalize_table_reference(sql, table_ref)
    if f"`{table_ref}`" not in sql:
        raise ValueError(f"Query must use the table exactly as `{table_ref}`.")

    ws = window_start.isoformat()
    we = window_end.isoformat()
    s_low = sql.lower()
    if f"date('{ws}')" not in s_low or f"date('{we}')" not in s_low:
        raise ValueError(
            f"Query must include the enforced window bounds DATE('{ws}') and DATE('{we}') as literals."
        )
    dc = target.date_column
    if not re.search(rf"date\s*\(\s*`?{re.escape(dc)}`?\s*\)", sql, re.I):
        raise ValueError(
            f"Query must filter with DATE({dc}) (or DATE(`{dc}`)) for the enforced window predicate."
        )
    return sql


def _dry_run_bytes(sql: str, project: str) -> int:
    client = bigquery.Client(project=project)
    job = client.query(
        sql,
        job_config=bigquery.QueryJobConfig(dry_run=True, use_query_cache=False),
    )
    return int(job.total_bytes_processed or 0)


def _run_query_json(sql: str, project: str, *, max_rows: int = MAX_RESULT_ROWS) -> list[dict[str, str]]:
    client = bigquery.Client(project=project)
    rows = list(client.query(sql).result(max_results=max_rows))
    out: list[dict[str, str]] = []
    for row in rows:
        rec: dict[str, str] = {}
        for key in row.keys():
            val = row.get(key)
            rec[str(key)] = "" if val is None else str(val)
        out.append(rec)
    return out


def run_llm_cost_sql_query(
    question: str,
    project: str,
    window_start: date,
    window_end: date,
    window_note: str,
    target: BqSqlTarget,
) -> tuple[str, str]:
    generated = _generate_billing_sql_generation(
        question,
        target,
        window_start,
        window_end,
        window_note,
    )
    sql = _validate_llm_sql(generated.sql, target, window_start, window_end)
    est = _dry_run_bytes(sql, project)
    cap = int(os.environ.get("BILLING_LLM_MAX_BYTES_BILLED", str(MAX_BYTES_DEFAULT)))
    if est > cap:
        raise RuntimeError(
            f"Dry-run estimate {est} bytes exceeds cap {cap}. Narrow the date range or filters."
        )
    rows = _run_query_json(sql, project)
    body = json.dumps(rows, ensure_ascii=False, indent=2)
    short = (
        f"llm-sql; target={target.date_column}; window={window_start}..{window_end}; "
        f"est_bytes={est}; sql_chars={len(sql)}"
    )
    return body, short


def run_llm_billing_query(
    question: str,
    table_ref: str,
    project: str,
    window_start: date,
    window_end: date,
    window_note: str,
) -> tuple[str, str]:
    return run_llm_cost_sql_query(
        question,
        project,
        window_start,
        window_end,
        window_note,
        gcp_billing_sql_target(table_ref),
    )
