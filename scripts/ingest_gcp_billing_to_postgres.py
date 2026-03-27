"""Ingest GCP Billing Export (BigQuery) into PostgreSQL cloud_costs table.

Usage example:
  python scripts/ingest_gcp_billing_to_postgres.py \
    --bq-project gls-training-486405 \
    --bq-dataset billing_export \
    --bq-table gcp_billing_export_v1_XXXX \
    --start-date 2026-03-01 \
    --end-date 2026-03-26

Required environment variables:
  DATABASE_URL   PostgreSQL DSN for cloud_costs table.

Auth:
  Uses Application Default Credentials for BigQuery (gcloud auth application-default login).
"""

from __future__ import annotations

import argparse
import datetime as dt
import os
from decimal import Decimal

import psycopg
from google.cloud import bigquery
from google.cloud.bigquery import ScalarQueryParameter


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--bq-project", required=True, help="BigQuery project id.")
    parser.add_argument("--bq-dataset", required=True, help="Billing export dataset.")
    parser.add_argument("--bq-table", required=True, help="Billing export table name.")
    parser.add_argument("--start-date", required=True, help="YYYY-MM-DD (inclusive).")
    parser.add_argument("--end-date", required=True, help="YYYY-MM-DD (inclusive).")
    parser.add_argument(
        "--default-environment",
        default="prod",
        choices=["prod", "dev"],
        help="Fallback environment when labels are missing.",
    )
    return parser.parse_args()


def normalize_environment(raw: str | None, default_environment: str) -> str:
    if not raw:
        return default_environment
    val = raw.strip().lower()
    if val in {"prod", "production", "prd"}:
        return "prod"
    if val in {"dev", "development"}:
        return "dev"
    # Keep table constraint satisfied.
    return default_environment


def fetch_aggregates(
    client: bigquery.Client,
    table_ref: str,
    start_date: str,
    end_date: str,
    default_environment: str,
) -> list[tuple[dt.date, str, str, Decimal]]:
    # Standard billing export query: daily/service/env aggregation.
    sql = f"""
    SELECT
      DATE(usage_start_time) AS usage_date,
      service.description AS service_name,
      COALESCE(
        (
          SELECT ANY_VALUE(l.value)
          FROM UNNEST(project.labels) AS l
          WHERE LOWER(l.key) IN ('environment', 'env')
        ),
        @default_environment
      ) AS raw_environment,
      SUM(cost) AS cost_usd
    FROM `{table_ref}`
    WHERE DATE(usage_start_time) BETWEEN @start_date AND @end_date
    GROUP BY usage_date, service_name, raw_environment
    ORDER BY usage_date DESC, service_name
    """
    job_config = bigquery.QueryJobConfig(
        query_parameters=[
            ScalarQueryParameter("start_date", "DATE", start_date),
            ScalarQueryParameter("end_date", "DATE", end_date),
            ScalarQueryParameter("default_environment", "STRING", default_environment),
        ]
    )
    rows = client.query(sql, job_config=job_config).result()
    out: list[tuple[dt.date, str, str, Decimal]] = []
    for row in rows:
        env = normalize_environment(row["raw_environment"], default_environment)
        out.append((row["usage_date"], row["service_name"], env, Decimal(row["cost_usd"])))
    return out


def write_to_postgres(
    dsn: str,
    start_date: str,
    end_date: str,
    aggregates: list[tuple[dt.date, str, str, Decimal]],
) -> None:
    with psycopg.connect(dsn) as conn:
        with conn.cursor() as cur:
            # Replace data for date range so repeated runs are idempotent.
            cur.execute(
                """
                DELETE FROM cloud_costs
                WHERE date BETWEEN %s::date AND %s::date
                """,
                (start_date, end_date),
            )
            if aggregates:
                cur.executemany(
                    """
                    INSERT INTO cloud_costs (date, service_name, environment, cost_usd)
                    VALUES (%s, %s, %s, %s)
                    """,
                    aggregates,
                )
        conn.commit()


def main() -> None:
    args = parse_args()
    database_url = os.environ.get("DATABASE_URL", "").strip()
    if not database_url:
        raise RuntimeError("DATABASE_URL is required.")

    # Validate date format early.
    dt.date.fromisoformat(args.start_date)
    dt.date.fromisoformat(args.end_date)

    table_ref = f"{args.bq_project}.{args.bq_dataset}.{args.bq_table}"
    bq_client = bigquery.Client(project=args.bq_project)
    aggregates = fetch_aggregates(
        bq_client,
        table_ref,
        args.start_date,
        args.end_date,
        args.default_environment,
    )
    write_to_postgres(database_url, args.start_date, args.end_date, aggregates)
    print(
        f"Ingest complete: {len(aggregates)} daily service rows loaded "
        f"for {args.start_date}..{args.end_date}."
    )


if __name__ == "__main__":
    main()
