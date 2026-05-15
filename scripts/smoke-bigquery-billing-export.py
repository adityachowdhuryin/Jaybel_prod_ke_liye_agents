#!/usr/bin/env python3
"""
M1: Verify Application Default Credentials can read the billing table and the
field shapes expected for the current BILLING_BQ_SCHEMA_MODE (`raw_export`:
nested standard export; `clean_view`: flat columns like service_name).

Usage (from repo root, after: gcloud auth application-default login):
  source config/gcp.env   # or export BQ_* / GOOGLE_CLOUD_PROJECT manually
  python scripts/smoke-bigquery-billing-export.py

Exit 0 on success; non-zero on failure.
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

# Load config/gcp.env without requiring shell source (optional)
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

from vertex_agents.cost_metrics_agent.billing_schema import is_clean_view_mode  # noqa: E402


def _aggregation_sql(table_ref: str) -> str:
    """Match standard export nesting vs. flat clean view (see billing_schema.py)."""
    if is_clean_view_mode():
        return f"""
    SELECT
      billing_account_id,
      ANY_VALUE(service_name) AS service_description,
      ANY_VALUE(project_id) AS project_id,
      ANY_VALUE(sku_description) AS sku_description,
      ANY_VALUE(region) AS region,
      ANY_VALUE(currency) AS currency,
      SUM(cost) AS total_cost
    FROM `{table_ref}`
    WHERE DATE(usage_start_time) BETWEEN DATE_SUB(CURRENT_DATE(), INTERVAL 7 DAY) AND CURRENT_DATE()
    GROUP BY billing_account_id
    LIMIT 5
    """
    return f"""
    SELECT
      billing_account_id,
      ANY_VALUE(service.description) AS service_description,
      ANY_VALUE(project.id) AS project_id,
      ANY_VALUE(sku.description) AS sku_description,
      ANY_VALUE(location.region) AS region,
      ANY_VALUE(currency) AS currency,
      SUM(cost) AS total_cost
    FROM `{table_ref}`
    WHERE DATE(usage_start_time) BETWEEN DATE_SUB(CURRENT_DATE(), INTERVAL 7 DAY) AND CURRENT_DATE()
    GROUP BY billing_account_id
    LIMIT 5
    """


def main() -> int:
    project = os.environ.get("BQ_BILLING_PROJECT") or os.environ.get("GOOGLE_CLOUD_PROJECT", "")
    dataset = os.environ.get("BQ_BILLING_DATASET", "")
    table = os.environ.get("BQ_BILLING_TABLE", "")
    if not all([project, dataset, table]):
        print("Set BQ_BILLING_PROJECT, BQ_BILLING_DATASET, BQ_BILLING_TABLE (or GOOGLE_CLOUD_PROJECT + BQ_*).")
        return 2

    table_ref = f"{project}.{dataset}.{table}"
    print(f">>> ADC project for jobs: {project}")
    print(f">>> Table: `{table_ref}`")

    client = bigquery.Client(project=project)

    # 1) Table metadata
    try:
        t = client.get_table(f"{project}.{dataset}.{table}")
        print(f">>> Table rows (approx): {t.num_rows}, bytes: {t.num_bytes}")
    except Exception as e:
        print(f"ERROR: get_table failed: {e}")
        return 1

    mode = "clean_view" if is_clean_view_mode() else "raw_export"
    print(f">>> BILLING_BQ_SCHEMA_MODE → smoke using `{mode}` field shapes")

    # 2) Aggregation with partition-friendly filter (last 7 days)
    sql = _aggregation_sql(table_ref)
    label = "clean-view columns" if is_clean_view_mode() else "nested export fields"
    print(f">>> Running 7-day smoke query ({label}, dry run)…")
    try:
        dr = client.query(
            sql,
            job_config=bigquery.QueryJobConfig(dry_run=True, use_query_cache=False),
        )
        print(f">>> Dry run total_bytes_processed: {dr.total_bytes_processed}")
    except Exception as e:
        print(f"ERROR: dry run failed: {e}")
        return 1

    print(">>> Running same query (execute, max 5 rows)…")
    try:
        rows = list(client.query(sql).result())
    except Exception as e:
        print(f"ERROR: query failed: {e}")
        return 1

    print(f">>> Row count: {len(rows)}")
    for i, row in enumerate(rows):
        print(f"    [{i}] {dict(row)}")

    # 3) Credits array presence (optional — may be empty in window)
    sql_credits = f"""
    SELECT COUNT(1) AS cnt
    FROM `{table_ref}`,
      UNNEST(IFNULL(credits, [])) AS c
    WHERE DATE(usage_start_time) BETWEEN DATE_SUB(CURRENT_DATE(), INTERVAL 30 DAY) AND CURRENT_DATE()
      AND ABS(IFNULL(c.amount, 0)) > 0
    LIMIT 1
    """
    try:
        cr = list(client.query(sql_credits).result())
        print(f">>> Rows with non-zero credit lines (30d): {cr[0]['cnt'] if cr else 0}")
    except Exception as e:
        print(f"WARN: credits probe skipped: {e}")

    print(">>> OK — ADC can read billing fields for this schema mode.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
