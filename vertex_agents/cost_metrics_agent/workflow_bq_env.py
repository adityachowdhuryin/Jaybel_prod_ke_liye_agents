"""Shared env resolution for the workflow BigQuery view (BQ_WORKFLOW_* with legacy BQ_COST_EVENTS_* fallback)."""

from __future__ import annotations

import os


def workflow_raw_table_name() -> str:
    return (os.environ.get("BQ_WORKFLOW_TABLE", "").strip() or os.environ.get("BQ_COST_EVENTS_TABLE", "").strip())


def workflow_table_configured(
    *,
    billing_project: str,
    billing_dataset: str,
) -> bool:
    t = workflow_raw_table_name()
    if not t:
        return False
    proj = (
        os.environ.get("BQ_WORKFLOW_PROJECT", "").strip()
        or os.environ.get("BQ_COST_EVENTS_PROJECT", "").strip()
        or billing_project
    )
    ds = (
        os.environ.get("BQ_WORKFLOW_DATASET", "").strip()
        or os.environ.get("BQ_COST_EVENTS_DATASET", "").strip()
        or billing_dataset
    )
    return bool(proj and ds)


def workflow_table_fqn(
    *,
    billing_project: str,
    billing_dataset: str,
) -> str | None:
    if not workflow_table_configured(billing_project=billing_project, billing_dataset=billing_dataset):
        return None
    proj = (
        os.environ.get("BQ_WORKFLOW_PROJECT", "").strip()
        or os.environ.get("BQ_COST_EVENTS_PROJECT", "").strip()
        or billing_project
    )
    ds = (
        os.environ.get("BQ_WORKFLOW_DATASET", "").strip()
        or os.environ.get("BQ_COST_EVENTS_DATASET", "").strip()
        or billing_dataset
    )
    table = workflow_raw_table_name()
    return f"{proj}.{ds}.{table}"
