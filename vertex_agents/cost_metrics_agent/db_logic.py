"""Read-only cost query helpers with dual backend support.

Backends:
- BigQuery Billing Export table (preferred when configured)
- PostgreSQL cloud_costs table (fallback)
"""

from __future__ import annotations

import json
import os
import re
from datetime import date
from decimal import Decimal

from google.cloud import bigquery
import psycopg

DATABASE_URL = os.environ.get(
    "DATABASE_URL",
    "postgresql://postgres:postgres@127.0.0.1:5433/postgres",
)
SOURCE_MODE = os.environ.get("COST_DATA_SOURCE", "auto").strip().lower()

# BigQuery Billing Export source (optional)
BQ_BILLING_PROJECT = os.environ.get("BQ_BILLING_PROJECT", "").strip()
BQ_BILLING_DATASET = os.environ.get("BQ_BILLING_DATASET", "").strip()
BQ_BILLING_TABLE = os.environ.get("BQ_BILLING_TABLE", "").strip()


def get_connection():
    return psycopg.connect(DATABASE_URL)


def _extract_filters(question: str) -> tuple[str | None, str | None, str | None, bool, str]:
    """Extract common filters from user question."""
    q = question.strip().lower()
    notes: list[str] = []
    env: str | None = None
    svc: str | None = None

    if "prod" in q or "production" in q:
        env = "prod"
        notes.append("filtering environment=prod")
    elif "dev" in q or "development" in q:
        env = "dev"
        notes.append("filtering environment=dev")

    svc_match = re.search(
        r"(compute engine|cloud storage|bigquery|cloud sql|artifact registry|networking|vertex ai|logging)",
        q,
        re.I,
    )
    if svc_match:
        svc = svc_match.group(1)
        notes.append(f"filtering service={svc}")

    date_match = re.search(r"20\d{2}-\d{2}-\d{2}", question)
    date_iso = date_match.group(0) if date_match else None
    if date_iso:
        notes.append(f"filtering date={date_iso}")

    wants_total = "total" in q or "sum" in q or "aggregate" in q
    reasoning = "; ".join(notes) or "no explicit filters, using full available data"
    return env, svc, date_iso, wants_total, reasoning


def nl_to_sql(question: str) -> tuple[str, str]:
    """Minimal, safe SELECT-only SQL for cloud_costs. Returns (sql, reasoning_snippet)."""
    env, svc, date_iso, wants_total, reasoning = _extract_filters(question)
    where: list[str] = []
    if env:
        where.append("environment = %s")
    if svc:
        where.append("LOWER(service_name) = LOWER(%s)")
    if date_iso:
        where.append("date = %s::date")

    if wants_total:
        wh = " AND ".join(where) if where else "TRUE"
        return (
            "SELECT COALESCE(SUM(cost_usd), 0) AS total_usd FROM cloud_costs WHERE " + wh,
            reasoning,
        )

    wh = " AND ".join(where) if where else "TRUE"
    return (
        "SELECT id, date, service_name, environment, cost_usd FROM cloud_costs WHERE "
        f"{wh} ORDER BY date DESC, id DESC LIMIT 100",
        reasoning,
    )


def params_for_sql(sql: str, question: str) -> tuple:
    params: list = []
    env, svc, date_iso, _, _ = _extract_filters(question)
    if env and "environment = %s" in sql:
        params.append(env)
    if svc and "LOWER(service_name)" in sql:
        params.append(svc)
    if date_iso and "date = %s::date" in sql:
        params.append(date_iso)
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


def _query_bigquery(question: str) -> str:
    env, svc, date_iso, wants_total, _ = _extract_filters(question)
    table_project = BQ_BILLING_PROJECT or os.environ.get("GOOGLE_CLOUD_PROJECT", "")
    if not table_project:
        raise RuntimeError("Set BQ_BILLING_PROJECT or GOOGLE_CLOUD_PROJECT for BigQuery source.")
    table_ref = f"{table_project}.{BQ_BILLING_DATASET}.{BQ_BILLING_TABLE}"

    filters: list[str] = []
    params: list[bigquery.ScalarQueryParameter] = []
    if svc:
        filters.append("LOWER(service.description) = LOWER(@service_name)")
        params.append(bigquery.ScalarQueryParameter("service_name", "STRING", svc))
    if date_iso:
        filters.append("DATE(usage_start_time) = @usage_date")
        params.append(bigquery.ScalarQueryParameter("usage_date", "DATE", date_iso))
    where_sql = f"WHERE {' AND '.join(filters)}" if filters else ""

    detail_sql = f"""
    SELECT
      DATE(usage_start_time) AS usage_date,
      service.description AS service_name,
      COALESCE(
        (
          SELECT ANY_VALUE(l.value)
          FROM UNNEST(project.labels) AS l
          WHERE LOWER(l.key) IN ('environment', 'env')
        ),
        'prod'
      ) AS raw_environment,
      SUM(cost) AS cost_usd
    FROM `{table_ref}`
    {where_sql}
    GROUP BY usage_date, service_name, raw_environment
    ORDER BY usage_date DESC, service_name
    LIMIT 100
    """
    total_sql = f"""
    SELECT COALESCE(SUM(cost), 0) AS total_usd
    FROM `{table_ref}`
    {where_sql}
    """
    sql = total_sql if wants_total else detail_sql
    client = bigquery.Client(project=table_project)
    rows = list(
        client.query(sql, job_config=bigquery.QueryJobConfig(query_parameters=params)).result()
    )
    if wants_total:
        total = rows[0]["total_usd"] if rows else Decimal("0")
        return json.dumps([{"total_usd": str(total)}], indent=2)

    normalized: list[dict[str, str]] = []
    for row in rows:
        row_env = _normalize_env(row["raw_environment"])
        if env and row_env != env:
            continue
        usage_date = row["usage_date"]
        usage_date_val = usage_date.isoformat() if isinstance(usage_date, date) else str(usage_date)
        normalized.append(
            {
                "date": usage_date_val,
                "service_name": str(row["service_name"]),
                "environment": row_env,
                "cost_usd": str(row["cost_usd"]),
            }
        )
    return json.dumps(normalized[:100], indent=2)


def _error_payload(kind: str, detail: str, hint: str | None = None) -> str:
    payload: dict[str, str] = {"error": kind, "detail": detail}
    if hint:
        payload["hint"] = hint
    return json.dumps(payload, indent=2)


def query_costs(question: str) -> str:
    """Query cloud costs from configured backend.

    Returns JSON rows on success, or a JSON object with ``error`` and ``detail``
    when the configured backend is unreachable or misconfigured (never raises).
    """
    mode = SOURCE_MODE if SOURCE_MODE in {"auto", "bigquery", "postgres"} else "auto"
    if mode in {"auto", "bigquery"} and _bigquery_ready():
        try:
            return _query_bigquery(question)
        except Exception as e:
            if mode == "bigquery":
                return _error_payload(
                    "bigquery_failed",
                    str(e),
                    "Check BQ_BILLING_* and IAM; table must exist and be readable.",
                )
            # auto: fall through to Postgres
    try:
        sql, _ = nl_to_sql(question)
        params = params_for_sql(sql, question)
        return run_query(sql, params)
    except Exception as e:
        return _error_payload(
            "postgres_query_failed",
            str(e),
            "Set DATABASE_URL to a reachable Postgres instance with cloud_costs. "
            "127.0.0.1 only works when a DB is bound locally (not in Agent Engine).",
        )
