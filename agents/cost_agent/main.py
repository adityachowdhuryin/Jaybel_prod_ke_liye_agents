"""
Cost Metrics Specialist — HTTP surface compatible with A2A-style discovery and /tasks/send SSE.

Data source modes:
- BigQuery billing export (preferred when configured)
- PostgreSQL cloud_costs table (fallback)
"""
from __future__ import annotations

import asyncio
import json
import os
import re
import uuid
from datetime import date
from decimal import Decimal
from typing import AsyncIterator

from google.cloud import bigquery
import psycopg
from fastapi import FastAPI, HTTPException
from fastapi.responses import JSONResponse, StreamingResponse
from pydantic import BaseModel, Field

from telemetry import setup_observability

# Optional: ADK agent shell for future tool wiring (no HTTP coupling)
try:
    from google.adk.agents import Agent
    from google.adk.runners import InMemoryRunner

    _ADK_AVAILABLE = True
except Exception:  # pragma: no cover
    _ADK_AVAILABLE = False

BASE_URL = os.environ.get("COST_AGENT_PUBLIC_URL", "http://localhost:8001")

# ---------------------------------------------------------------------------
# DATABASE_URL (required in cloud; optional locally)
#
# Local dev: defaults to postgres on localhost (see below).
#
# Production (Phase 2 — hybrid cloud): compute runs on GCP (Cloud Run / Agent
# Engine), but PostgreSQL stays on-premises. The app does NOT open inbound DB
# ports on GCP; instead, expose the on-prem Postgres (or a TCP proxy in front
# of it) through a secure tunnel such as Cloudflare Tunnel, Tailscale Funnel,
# or ngrok TCP. Set DATABASE_URL to the tunnel's public DSN, for example:
#   postgresql://user:pass@db-tunnel.example.com:5432/postgres
#
# Store this value in Secret Manager and mount it into Cloud Run with
# --set-secrets (see deploy.sh). Rotate tunnel credentials independently of
# the service image.
# ---------------------------------------------------------------------------
DATABASE_URL = os.environ.get(
    "DATABASE_URL",
    "postgresql://postgres:postgres@localhost:5432/postgres",
)
SOURCE_MODE = os.environ.get("COST_DATA_SOURCE", "auto").strip().lower()
BQ_BILLING_PROJECT = os.environ.get("BQ_BILLING_PROJECT", "").strip()
BQ_BILLING_DATASET = os.environ.get("BQ_BILLING_DATASET", "").strip()
BQ_BILLING_TABLE = os.environ.get("BQ_BILLING_TABLE", "").strip()


def get_connection():
    return psycopg.connect(DATABASE_URL)


def _extract_filters(question: str) -> tuple[str | None, str | None, str | None, bool, str]:
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
    return env, svc, date_iso, wants_total, "; ".join(notes) or "no explicit filters"


def nl_to_sql(question: str) -> tuple[str, str]:
    env, svc, date_iso, wants_total, hint = _extract_filters(question)
    where: list[str] = []
    if env:
        where.append("environment = %s")
    if svc:
        where.append("LOWER(service_name) = LOWER(%s)")
    if date_iso:
        where.append("date = %s::date")
    wh = " AND ".join(where) if where else "TRUE"
    if wants_total:
        return (
            "SELECT COALESCE(SUM(cost_usd), 0) AS total_usd FROM cloud_costs WHERE " + wh,
            hint,
        )
    return (
        f"SELECT id, date, service_name, environment, cost_usd FROM cloud_costs WHERE {wh} ORDER BY date DESC, id DESC LIMIT 100",
        hint,
    )


def _params_for_sql(sql: str, question: str) -> tuple:
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


def query_bigquery(question: str) -> str:
    env, svc, date_iso, wants_total, _ = _extract_filters(question)
    table_project = BQ_BILLING_PROJECT or os.environ.get("GOOGLE_CLOUD_PROJECT", "")
    if not table_project:
        raise RuntimeError("Set BQ_BILLING_PROJECT or GOOGLE_CLOUD_PROJECT for BigQuery queries.")
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
    if wants_total:
        sql = f"SELECT COALESCE(SUM(cost), 0) AS total_usd FROM `{table_ref}` {where_sql}"
    else:
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
            'prod'
          ) AS raw_environment,
          SUM(cost) AS cost_usd
        FROM `{table_ref}`
        {where_sql}
        GROUP BY usage_date, service_name, raw_environment
        ORDER BY usage_date DESC, service_name
        LIMIT 100
        """
    client = bigquery.Client(project=table_project)
    rows = list(
        client.query(sql, job_config=bigquery.QueryJobConfig(query_parameters=params)).result()
    )
    if wants_total:
        total = rows[0]["total_usd"] if rows else Decimal("0")
        return json.dumps([{"total_usd": str(total)}], indent=2)
    out: list[dict[str, str]] = []
    for row in rows:
        row_env = _normalize_env(row["raw_environment"])
        if env and row_env != env:
            continue
        usage_date = row["usage_date"]
        out.append(
            {
                "date": usage_date.isoformat() if isinstance(usage_date, date) else str(usage_date),
                "service_name": str(row["service_name"]),
                "environment": row_env,
                "cost_usd": str(row["cost_usd"]),
            }
        )
    return json.dumps(out[:100], indent=2)


def query_cost_data(question: str) -> tuple[str, str]:
    """Run query against configured source, with fallback to Postgres in auto mode."""
    mode = SOURCE_MODE if SOURCE_MODE in {"auto", "bigquery", "postgres"} else "auto"
    env, svc, date_iso, _, hint = _extract_filters(question)
    _ = (env, svc, date_iso)
    if mode in {"auto", "bigquery"} and _bigquery_ready():
        try:
            return query_bigquery(question), f"{hint}; source=bigquery"
        except Exception as e:
            if mode == "bigquery":
                raise
            hint = f"{hint}; bigquery unavailable ({e}); fallback=postgres"
    sql, _ = nl_to_sql(question)
    params = _params_for_sql(sql, question)
    return run_query(sql, params), f"{hint}; source=postgres"


def agent_card() -> dict:
    return {
        "name": "Cost Metrics Agent",
        "description": "Enterprise tasks: query infrastructure costs, analyze usage spikes, generate budget reports.",
        "url": BASE_URL,
        "version": "1.0.0",
        "capabilities": {"streaming": True, "pushNotifications": False},
        "skills": [
            {
                "id": "metrics.query_cost",
                "name": "Cost Query",
                "description": "Query costs by service, date, or environment.",
                "inputModes": ["text"],
                "outputModes": ["text"],
            }
        ],
    }


class TaskSendBody(BaseModel):
    message: str = Field(..., description="Natural language cost question")
    id: str | None = Field(default=None, description="Optional task id")


app = FastAPI(title="Cost Metrics Agent", version="1.0.0")

if _ADK_AVAILABLE:
    _cost_adk_agent = Agent(
        model="gemini-2.0-flash",
        name="cost_metrics_adk",
        description="ADK agent placeholder; HTTP layer performs NL→SQL for Phase 1.",
    )
    _ = InMemoryRunner(agent=_cost_adk_agent)


@app.get("/.well-known/agent.json")
async def well_known_agent():
    return JSONResponse(agent_card())


def sse_pack(obj: dict) -> str:
    return f"data: {json.dumps(obj, ensure_ascii=False)}\n\n"


async def task_stream(message: str, task_id: str) -> AsyncIterator[str]:
    yield sse_pack(
        {
            "id": task_id,
            "status": {
                "state": "working",
                "message": {
                    "role": "agent",
                    "parts": [{"text": "Parsing your question and generating SQL…"}],
                },
            },
        }
    )
    await asyncio.sleep(0.05)

    try:
        _, hint = nl_to_sql(message)
    except Exception as e:  # pragma: no cover
        yield sse_pack(
            {
                "id": task_id,
                "status": {"state": "failed", "message": str(e)},
            }
        )
        return

    yield sse_pack(
        {
            "id": task_id,
            "status": {
                "state": "working",
                "message": {
                    "role": "agent",
                    "parts": [{"text": f"Running query ({hint})…"}],
                },
            },
        }
    )
    await asyncio.sleep(0.05)

    try:
        result_text, source_hint = await asyncio.to_thread(query_cost_data, message)
    except Exception as e:
        yield sse_pack(
            {
                "id": task_id,
                "status": {
                    "state": "working",
                    "message": {
                        "role": "agent",
                        "parts": [{"text": f"Database error: {e}"}],
                    },
                },
            }
        )
        yield sse_pack({"id": task_id, "status": {"state": "completed"}, "artifact": {"parts": [{"text": ""}]}})
        return

    # Stream result in chunks (dummy chunking for SSE demo)
    chunk_size = 180
    summary = f"Source: {source_hint}\n\nResult:\n{result_text}"
    for i in range(0, len(summary), chunk_size):
        part = summary[i : i + chunk_size]
        yield sse_pack(
            {
                "id": task_id,
                "status": {
                    "state": "working",
                    "message": {
                        "role": "agent",
                        "parts": [{"text": part}],
                    },
                },
            }
        )
        await asyncio.sleep(0.02)

    yield sse_pack(
        {
            "id": task_id,
            "status": {"state": "completed"},
            "artifact": {"parts": [{"text": f"\n\n✓ Completed ({hint})."}]},
        }
    )


@app.post("/tasks/send")
async def tasks_send(body: TaskSendBody):
    task_id = body.id or f"task-{uuid.uuid4().hex[:12]}"
    if not body.message.strip():
        raise HTTPException(status_code=400, detail="message is required")

    return StreamingResponse(
        task_stream(body.message, task_id),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )


@app.get("/health")
async def health():
    return {
        "status": "ok",
        "adk": _ADK_AVAILABLE,
        "source_mode": SOURCE_MODE,
        "bigquery_configured": _bigquery_ready(),
    }


setup_observability(app, "cost-agent")
