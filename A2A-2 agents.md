**System Architecture & Implementation Guide: Cost Intelligence Stack (Current)**

## 1) Executive Summary

This project now runs in an **Agent Engine-only execution model** for cost intelligence:

- A **Next.js frontend** for chat, session history, and streaming UI.
- A **FastAPI orchestrator bridge** for auth/session persistence and SSE bridging.
- A deployed **Vertex Agent Engine orchestrator agent**.
- A deployed **Vertex Agent Engine cost metrics agent**.
- **Gemini 2.5 Flash** as the primary LLM model family for routing and SQL generation.

The retired local specialist execution path (`agents/cost_agent`) has been removed.

## 2) Current Technology Stack

- Frontend: `Next.js` (App Router), `Tailwind`, `shadcn/ui`
- Local backend bridge: `FastAPI` (`agents/orchestrator`)
- Session store: `PostgreSQL 18`
- Cloud execution: `Vertex AI Agent Engine` (`pa_orchestrator_agent`, `cost_metrics_agent`)
- Data source: `BigQuery` view `gls-training-486405.gcp_billing_data.clean_billing_view`
- Streaming: SSE (`/chat/stream`) from bridge to browser

## 3) Runtime Topology (Local Dev)

- Frontend: `http://127.0.0.1:3000`
- Orchestrator bridge: `http://127.0.0.1:8000`
- Postgres (docker): `127.0.0.1:5435`

Startup:

- `bash scripts/start-all.sh`

What it starts now:

- Postgres container + schema setup
- Orchestrator bridge (Agent Engine-only mode)
- Next.js frontend

## 4) Active Execution Path

1. UI sends chat to local `/api/chat/stream`.
2. Next.js route proxies to local orchestrator bridge `/chat/stream`.
3. Bridge forwards to deployed Vertex Agent Engine orchestrator (`stream_query`).
4. Orchestrator agent invokes deployed cost agent as needed.
5. Streamed results are normalized and forwarded back to UI.

There is no local `/tasks/send` specialist path anymore.

## 5) Core Components

### A) Local Orchestrator Bridge (`agents/orchestrator`)

- Persists chat sessions/messages/summaries in Postgres.
- Maintains idempotency via `client_message_id`.
- Enforces Agent Engine-only chat path (returns `503` if Agent Engine is unavailable).
- Provides session APIs:
  - `/chat/sessions`
  - `/chat/sessions/{id}/messages`
  - `/chat/sessions/{id}` (delete)
  - `/chat/sessions/{id}/export`

### B) Deployed Orchestrator Agent (`vertex_agents/pa_orchestrator_agent`)

- Routes cost and billing/schema questions to cost specialist tool.
- Avoids speculation; asks clarification when needed.
- Summarizes specialist output for user-facing responses.

### C) Deployed Cost Agent (`vertex_agents/cost_metrics_agent`)

- BigQuery-first execution (`COST_DATA_SOURCE=bigquery`).
- LLM-first cost query pipeline:
  - context routing (structured JSON)
  - guarded SQL generation (structured JSON)
  - strict SQL validation + dry-run bytes guard
- BigQuery schema introspection path:
  - list columns
  - check if column exists
  - distinct values for supported scalar columns

## 6) BigQuery Source & Schema Mode

Configured source:

- Project: `gls-training-486405`
- Dataset: `gcp_billing_data`
- View: `clean_billing_view`
- Mode: `BILLING_BQ_SCHEMA_MODE=clean_view`

## 7) Guardrails & Clarification Behavior

- Clarification-first for ambiguous requests (time scope, top-N, compare scope).
- SQL constraints:
  - single statement
  - SELECT/CTE only
  - enforced table reference
  - enforced date window literals
- Cost control:
  - dry-run estimated bytes
  - `BILLING_LLM_MAX_BYTES_BILLED`
- Schema query safety:
  - unknown column -> explicit error
  - unsupported nested distinct queries -> explicit guidance

## 8) Key Environment Flags

- `ORCHESTRATOR_AGENT_ENGINE_RESOURCE=projects/.../reasoningEngines/...`
- `ORCHESTRATOR_LOCAL_CHAT=0` (or unset)
- `BQ_BILLING_PROJECT=gls-training-486405`
- `BQ_BILLING_DATASET=gcp_billing_data`
- `BQ_BILLING_TABLE=clean_billing_view`
- `BILLING_BQ_SCHEMA_MODE=clean_view`
- `COST_DATA_SOURCE=bigquery`
- `BILLING_AGENT_LLM_SQL=1`
- `BILLING_CONTEXT_ROUTER_ENABLED=1`
- `BILLING_LLM_PROVIDER=auto`
- `BILLING_LLM_MAX_BYTES_BILLED=1000000000` (example)
- `BILLING_LLM_MAX_LOOKBACK_DAYS=0` (recommended in this setup)
- `BILLING_DEFAULT_TILL_NOW_SCOPE=full_history`
- `BILLING_FULL_HISTORY_START_DATE=2026-01-01`

## 9) Operational Status

- Agent Engine-only execution: implemented
- Local specialist execution path: removed
- Chat persistence + summaries: implemented
- Sidebar session history/new/delete/pagination: implemented
- Schema introspection against live BigQuery view: implemented
- Structured routing + structured SQL generation + guardrails: implemented
