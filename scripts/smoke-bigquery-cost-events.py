#!/usr/bin/env python3
"""Verify ADC can query workflow view (BQ_WORKFLOW_* or legacy BQ_COST_EVENTS_*): date window + cost_usd."""
from __future__ import annotations

import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

_gcp_env = ROOT / "config" / "gcp.env"
if _gcp_env.is_file():
    for line in _gcp_env.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        if line.startswith("export "):
            line = line[7:]
        k, _, v = line.partition("=")
        k, v = k.strip(), v.strip().strip('"').strip("'")
        if k and k not in os.environ:
            os.environ[k] = v

from google.cloud import bigquery  # noqa: E402


def _table_parts() -> tuple[str, str, str] | None:
    project = (
        os.environ.get("BQ_WORKFLOW_PROJECT")
        or os.environ.get("BQ_COST_EVENTS_PROJECT")
        or os.environ.get("BQ_BILLING_PROJECT")
        or os.environ.get("GOOGLE_CLOUD_PROJECT", "")
    ).strip()
    dataset = (
        os.environ.get("BQ_WORKFLOW_DATASET")
        or os.environ.get("BQ_COST_EVENTS_DATASET")
        or os.environ.get("BQ_BILLING_DATASET", "")
    ).strip()
    table = (
        os.environ.get("BQ_WORKFLOW_TABLE", "").strip()
        or os.environ.get("BQ_COST_EVENTS_TABLE", "").strip()
    )
    if not table or not project or not dataset:
        return None
    return project, dataset, table


def main() -> int:
    parts = _table_parts()
    if not parts:
        print("Skip: set BQ_WORKFLOW_TABLE (or legacy BQ_COST_EVENTS_TABLE) with project/dataset.")
        return 0
    project, dataset, table = parts
    table_ref = f"{project}.{dataset}.{table}"
    print(f">>> Table: `{table_ref}`")
    client = bigquery.Client(project=project)

    sql = f"""
    SELECT
      COUNT(1) AS row_count,
      SUM(IFNULL(cost_usd, 0)) AS total_usd
    FROM `{table_ref}`
    WHERE DATE(timestamp) BETWEEN DATE_SUB(CURRENT_DATE(), INTERVAL 7 DAY) AND CURRENT_DATE()
    """
    print(">>> Dry run…")
    try:
        dr = client.query(
            sql,
            job_config=bigquery.QueryJobConfig(dry_run=True, use_query_cache=False),
        )
        print(f">>> Dry run bytes: {dr.total_bytes_processed}")
    except Exception as e:
        print(f"ERROR: {e}")
        return 1
    print(">>> Execute…")
    try:
        row = next(iter(client.query(sql).result()))
        print(f">>> row_count={row['row_count']}, total_usd={row['total_usd']}")
    except Exception as e:
        print(f"ERROR: {e}")
        return 1
    print(">>> OK")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
