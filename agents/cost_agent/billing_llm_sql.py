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
from datetime import date
from typing import Any

from google.cloud import bigquery
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

MAX_BYTES_DEFAULT = 1_000_000_000
MAX_RESULT_ROWS = 512


class BillingSqlGeneration(BaseModel):
    """VERTEX / Google AI JSON response; validated after parse (not sent as schema $ref)."""

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


# Gemini structured-output subset: explicit object + properties (no Pydantic $defs).
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
    """Import-level readiness (Vertex SDK and/or API key present). Runtime IAM may still deny Vertex."""
    return _VERTEX_OK or google_ai_configured()


def _is_vertex_permission_error(exc: BaseException) -> bool:
    s = str(exc).lower()
    return "403" in s or "permission_denied" in s or "iam_permission_denied" in s.replace(" ", "_")


def _build_sql_prompt(
    table_ref: str,
    window_start: date,
    window_end: date,
    window_note: str,
    question: str,
) -> str:
    ws, we = window_start.isoformat(), window_end.isoformat()
    schema = f"""
Allowed table (only source): `{table_ref}`

Standard resource-level export (nested fields):
- usage_start_time TIMESTAMP — required in WHERE as DATE(usage_start_time) BETWEEN ...
- cost FLOAT64, currency STRING (this dataset uses **INR**)
- service.id, service.description
- sku.id, sku.description
- project.id, project.name, project.labels (ARRAY)
- location.region, location.zone, location.country
- cost_type STRING
- credits ARRAY<STRUCT<...>> — UNNEST(credits) for credit lines (amount, name, etc.)
"""
    return f"""You are a BigQuery analyst for GCP billing exports.

{schema}

Hard requirements:
1. A single statement only: WITH ... SELECT ... or plain SELECT. No DDL/DML, no multi-statement.
2. FROM / JOIN must only reference `{table_ref}` (UNNEST of its columns is allowed).
3. WHERE must include exactly this predicate (you may AND more conditions after it):
   DATE(usage_start_time) BETWEEN DATE('{ws}') AND DATE('{we}')
4. Use explicit DATE('YYYY-MM-DD') literals for those bounds (do not use parameters).
5. Prefer aggregates; for wide scans add LIMIT {MAX_RESULT_ROWS} or less.
6. Amounts: SUM(cost); alias totals as total_inr or similar. Mention INR in column names where helpful.
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
    table_ref: str,
    window_start: date,
    window_end: date,
    window_note: str,
) -> BillingSqlGeneration:
    prompt = _build_sql_prompt(table_ref, window_start, window_end, window_note, question)
    provider = os.environ.get("BILLING_LLM_PROVIDER", "auto").strip().lower()
    key = google_ai_api_key()

    if provider == "google_ai":
        return _invoke_google_ai(prompt)
    if provider == "vertex":
        return _invoke_vertex(prompt)

    # auto: Vertex first, then Google AI on permission errors
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
            "Vertex AI denied access (aiplatform.endpoints.predict). "
            "Grant your user roles/aiplatform.user on the project, or set GOOGLE_AI_API_KEY / GEMINI_API_KEY "
            "for Google AI Studio fallback."
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
    """
    Gemini may return an unquoted fully-qualified table name; normalize it to a single
    backticked form so strict policy checks and BigQuery syntax both succeed.
    """
    tick = f"`{table_ref}`"
    if tick in sql:
        return sql
    # Accept and normalize explicit component quoting: `p`.`d`.`t` -> `p.d.t`
    bits = table_ref.split(".")
    if len(bits) == 3:
        dotted_tick = f"`{bits[0]}`.`{bits[1]}`.`{bits[2]}`"
        if dotted_tick in sql:
            return sql.replace(dotted_tick, tick)
    # Accept and normalize bare project.dataset.table if exact.
    if table_ref in sql:
        return sql.replace(table_ref, tick)
    return sql


def _validate_llm_sql(sql_raw: str, table_ref: str, window_start: date, window_end: date) -> str:
    sql = _first_statement(sql_raw)
    if not sql:
        raise ValueError("Empty SQL after extraction.")
    lead = sql.lstrip()
    lu = lead.upper()
    if not (lu.startswith("SELECT") or lu.startswith("WITH")):
        raise ValueError("Only SELECT (or WITH ... SELECT) queries are allowed.")
    probe = _strip_sql_comments(sql)
    if _FORBIDDEN.search(probe):
        raise ValueError("Disallowed SQL keyword detected.")
    sql = _normalize_table_reference(sql, table_ref)
    tick = f"`{table_ref}`"
    if tick not in sql:
        raise ValueError(f"Query must use the billing table exactly as {tick}.")
    pl = probe.lower()
    if "usage_start_time" not in pl and "_partitiontime" not in pl and "_partition_time" not in pl:
        raise ValueError("Query must reference usage_start_time (or _PARTITIONTIME) for partitioning.")
    ws, we = window_start.isoformat(), window_end.isoformat()
    if ws not in sql or we not in sql:
        raise ValueError(
            f"Query must include the enforced window bounds DATE('{ws}') and DATE('{we}') as literals."
        )
    limits = [int(m.group(1)) for m in re.finditer(r"\bLIMIT\s+(\d+)\b", sql, re.I)]
    if limits and max(limits) > MAX_RESULT_ROWS:
        raise ValueError(f"LIMIT must be <= {MAX_RESULT_ROWS}.")
    return sql


def generate_sql(
    question: str,
    table_ref: str,
    window_start: date,
    window_end: date,
    window_note: str,
) -> str:
    if not llm_sql_usable():
        raise RuntimeError(
            "No LLM SQL backend: install google-cloud-aiplatform and/or google-generativeai "
            "and set GOOGLE_AI_API_KEY if not using Vertex."
        )
    payload = _generate_billing_sql_generation(
        question, table_ref, window_start, window_end, window_note
    )
    sql = payload.sql.replace("\ufeff", "").strip()
    return _validate_llm_sql(sql, table_ref, window_start, window_end)


def run_llm_billing_query(
    question: str,
    table_ref: str,
    job_project: str,
    window_start: date,
    window_end: date,
    window_note: str,
) -> tuple[str, str]:
    """Returns (body text for the user, short source hint for logging/UI)."""
    max_bytes = int(os.environ.get("BILLING_LLM_MAX_BYTES_BILLED", str(MAX_BYTES_DEFAULT)))
    sql = generate_sql(question, table_ref, window_start, window_end, window_note)

    client = bigquery.Client(project=job_project)
    dry_cfg = bigquery.QueryJobConfig(dry_run=True, use_query_cache=False)
    try:
        dj = client.query(sql, job_config=dry_cfg)
        bytes_est = dj.total_bytes_processed or 0
    except Exception as e:
        return (
            json.dumps(
                {
                    "error": "dry_run_failed",
                    "detail": str(e),
                    "sql_preview": sql[:2000],
                },
                indent=2,
            ),
            "llm-sql dry-run failed",
        )

    if bytes_est > max_bytes:
        msg = (
            "This query would scan about "
            f"{bytes_est / 1e9:.2f} GB, which exceeds your maximum of {max_bytes / 1e9:.1f} GB. "
            "Please narrow the time range (for example a single week), add filters such as project id, "
            "service, or SKU, or ask for a smaller breakdown."
        )
        return (
            json.dumps(
                {"error": "bytes_limit", "message": msg, "estimated_bytes": bytes_est},
                indent=2,
            ),
            "llm-sql aborted (dry-run bytes)",
        )

    exec_cfg = bigquery.QueryJobConfig(
        maximum_bytes_billed=max_bytes,
        use_query_cache=False,
    )
    try:
        rows = list(client.query(sql, job_config=exec_cfg).result())
    except Exception as e:
        err_s = str(e)
        if "bytes billed" in err_s.lower() or "maximum bytes billed" in err_s.lower():
            return (
                json.dumps(
                    {
                        "error": "execution_bytes_cap",
                        "message": "The query hit the maximum bytes billed cap. Try a shorter date range or more specific filters.",
                        "detail": err_s,
                    },
                    indent=2,
                ),
                "llm-sql execution capped",
            )
        return (
            json.dumps({"error": "execution_failed", "detail": err_s}, indent=2),
            "llm-sql execution error",
        )

    out: list[dict[str, str]] = []
    for row in rows:
        out.append({k: str(v) for k, v in dict(row).items()})
    body = json.dumps(out[:MAX_RESULT_ROWS], indent=2)
    note_prefix = f"Note: {window_note}\n\n" if window_note else ""
    full = note_prefix + "Currency: INR (₹).\n\n" + body
    return (
        full,
        f"llm-sql; window={window_start}..{window_end}; est_bytes={bytes_est}; sql_chars={len(sql)}",
    )
