"""Short BigQuery column digests from live table metadata (API-driven, no intent regex)."""

from __future__ import annotations

import os
from typing import Any

from google.cloud import bigquery

from .workflow_bq_env import workflow_raw_table_name, workflow_table_fqn

_MAX_FIELDS = 48
_MAX_DEPTH = 3


def _field_lines(fields: list[Any], prefix: str = "", depth: int = 0) -> list[str]:
    out: list[str] = []
    if depth > _MAX_DEPTH:
        return out
    for f in fields:
        name = f"{prefix}{f.name}"
        ft = str(getattr(f, "field_type", "") or "")
        line = f"- {name} ({ft})"
        out.append(line)
        nested = getattr(f, "fields", None) or []
        if nested:
            out.extend(_field_lines(list(nested), prefix=f"{name}.", depth=depth + 1))
    return out


def _digest_for_table(client: bigquery.Client, table_ref: str, label: str) -> str:
    try:
        t = client.get_table(table_ref)
        lines = _field_lines(list(t.schema))[:_MAX_FIELDS]
        body = "\n".join(lines) if lines else "(no schema fields)"
        return f"{label} `{table_ref}`:\n{body}"
    except Exception as e:
        return f"{label} `{table_ref}`: (metadata unavailable: {e})"


def build_dual_table_schema_digest() -> str:
    """Return a compact markdown block for router + SQL prompts. Empty if billing project missing."""
    project = (
        os.environ.get("BQ_BILLING_PROJECT")
        or os.environ.get("GOOGLE_CLOUD_PROJECT", "")
    ).strip()
    if not project:
        return ""
    client = bigquery.Client(project=project)
    parts: list[str] = []
    ds = os.environ.get("BQ_BILLING_DATASET", "").strip()
    bt = os.environ.get("BQ_BILLING_TABLE", "").strip()
    if ds and bt:
        parts.append(_digest_for_table(client, f"{project}.{ds}.{bt}", "GCP billing"))
    wf_ref = workflow_table_fqn(billing_project=project, billing_dataset=ds)
    if wf_ref:
        label = f"Workflow view ({workflow_raw_table_name()})"
        parts.append(_digest_for_table(client, wf_ref, label))
    return "\n\n".join(parts) if parts else ""
