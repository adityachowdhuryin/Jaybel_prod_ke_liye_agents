**System Architecture & Implementation Guide: Cost Intelligence A2A Stack (Current)**

## 1) Executive Summary

This project implements a local-first, production-ready conversational system for cloud cost analytics:

- A **Next.js frontend** for chat, session history, and streaming UI.
- A **FastAPI Orchestrator** for session memory, intent routing, and specialist dispatch.
- A **FastAPI Cost Agent** for billing analytics via BigQuery with guarded LLM SQL.
- **A2A-style SSE contract** between orchestrator and specialist agent.
- **Vertex Gemini 2.5 Flash** used for routing, context interpretation, summarization, and structured SQL generation.

The system now uses a **clean billing BigQuery view** (`clean_billing_view`) in schema-aware mode to reduce irrelevant fields and improve reliability.

## 2) Current Technology Stack

- Frontend: `Next.js` (App Router), `Tailwind`, `shadcn/ui`
- Backend APIs: `FastAPI` (orchestrator + cost agent)
- Database (chat memory): `PostgreSQL 18`
- Cost data source: `BigQuery` billing dataset/view
- LLMs: `gemini-2.5-flash` on Vertex (with optional Google AI fallback for billing SQL path)
- Streaming: SSE end-to-end (`/chat/stream` and `/tasks/send`)

## 3) Runtime Topology (Local Dev)

- Frontend: `http://127.0.0.1:3000`
- Orchestrator: `http://127.0.0.1:8000`
- Cost Agent: `http://127.0.0.1:8001`
- Postgres (docker): host `127.0.0.1:5435`

Startup command:

- `bash scripts/start-all.sh`

This script:

- starts Postgres container and applies schemas/migrations
- starts cost-agent and orchestrator
- starts Next.js frontend
- writes `frontend/.env.development.local`

## 4) Core Components

### A) Orchestrator (Conversational Brain)

- Maintains chat sessions/messages in Postgres.
- Performs intent routing (`metrics`, `chitchat`, `clear`, `out_of_scope`) using Gemini JSON routing when enabled.
- Compresses long histories via Gemini summary before routing.
- Dispatches cost requests to specialist `/tasks/send`.
- Streams back A2A-shaped SSE events to the frontend.

### B) Cost Agent (Billing Specialist)

- Main path: BigQuery + guarded LLM-generated SQL.
- Context router (Gemini JSON) extracts:
  - time window
  - filters (service/project/region/env)
  - query intent hints (total/top/list)
- SQL generation (Gemini JSON) + strict validation:
  - SELECT-only
  - required table reference
  - enforced date window literals
  - bytes dry-run + max billed cap

### C) Frontend

- Chat panel with optimistic user + assistant streaming.
- Desktop sidebar with:
  - list sessions
  - new chat
  - delete chat
  - pagination
- Uses Next.js API proxy routes for orchestrator health/chat/session APIs.

## 5) Memory & Session Model

- Primary persistent memory is in Orchestrator Postgres tables:
  - sessions
  - session messages
  - summaries
- Cost agent does not maintain separate long-term memory; it uses routed context + rewritten prompt from orchestrator flow.

## 6) A2A Contract (Implemented)

### Specialist discovery

- `GET /.well-known/agent.json`

### Specialist execution (streaming)

- `POST /tasks/send`
- returns SSE chunks with:
  - working status + partial text
  - completed status + artifact text

This format is consumed by orchestrator/frontend A2A parsing logic.

## 7) BigQuery Source & Schema Mode (Current)

The cost agent is now configured to query:

- Project: `gls-training-486405`
- Dataset: `gcp_billing_data`
- Table/View: `clean_billing_view`

Schema mode:

- `BILLING_BQ_SCHEMA_MODE=clean_view`

This mode maps filters/prompts to clean columns (e.g. `service_name`, `project_id`, `region`, `project_labels`) instead of raw nested export fields.

## 8) Time Window Policy (Current)

- Day-cap can be disabled with:
  - `BILLING_LLM_MAX_LOOKBACK_DAYS=0`
- Byte-cap guardrail still enforced via:
  - dry-run estimate
  - `BILLING_LLM_MAX_BYTES_BILLED`
- Full-month explicit windows are preserved (not truncated).
- Till-now phrases are normalized (`till now`, `until now`, `till date`, `to date`, `so far`) and policy-driven:
  - `BILLING_DEFAULT_TILL_NOW_SCOPE=full_history`
  - `BILLING_FULL_HISTORY_START_DATE=2026-01-01`

## 9) Key Environment Flags

- `ENABLE_VERTEX_ROUTING=1` (orchestrator Gemini routing)
- `BILLING_AGENT_LLM_SQL=1` (LLM SQL path on)
- `BILLING_CONTEXT_ROUTER_ENABLED=1` (cost context router on)
- `BILLING_BQ_SCHEMA_MODE=clean_view`
- `BQ_BILLING_TABLE=clean_billing_view`
- `BILLING_LLM_MAX_BYTES_BILLED=...`
- `BILLING_LLM_MAX_LOOKBACK_DAYS=0` (current local policy)
- `BILLING_DEFAULT_TILL_NOW_SCOPE=full_history`
- `BILLING_FULL_HISTORY_START_DATE=2026-01-01`

## 10) Operational Notes

- Health endpoints:
  - orchestrator: `/health`
  - cost agent: `/health`
- Frontend proxy mode is enabled to avoid browser CORS/auth issues.
- For cloud/hybrid deployment, DB tunnel/secret strategy remains supported by environment configuration.

## 11) Current Implementation Status (High-level)

- Chat persistence and summaries: implemented
- Sidebar session history/new/delete/pagination: implemented
- Structured JSON routing + structured JSON SQL generation: implemented
- Clean billing view mode + column mapping: implemented
- Guardrails (SELECT-only, table/date checks, bytes cap): implemented
