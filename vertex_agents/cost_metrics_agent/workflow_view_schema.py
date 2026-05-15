"""LLM-facing schema for the curated workflow / runtime BigQuery view (flat columns, not Logging jsonPayload)."""

from __future__ import annotations


def llm_workflow_view_schema_description(table_ref: str) -> str:
    return """
Allowed table (only source): `{table_ref}`

Curated workflow / agent-runtime view (flat columns — no nested jsonPayload):
- `timestamp` TIMESTAMP — partition/time field; use in WHERE as DATE(timestamp) BETWEEN @start AND @end (inclusive).
- `trace_id` STRING — correlation id for a single agent run / trace; filter with exact match or LOWER(TRIM(trace_id)) when comparing ids the user pasted.
- `cost_usd` FLOAT64 — spend for the row in **USD**; use SUM(cost_usd) for totals; alias as total_usd where helpful.
- `input_tokens` FLOAT64 — input token count for the row (nullable).
- `output_tokens` FLOAT64 — output token count for the row (nullable).

Do NOT use GCP billing export column names (`usage_start_time`, `cost`, `service_name`, `invoice_month`, etc.) on this table.
Do NOT reference jsonPayload.* — those fields do not exist on this view.
""".strip().format(table_ref=table_ref)
