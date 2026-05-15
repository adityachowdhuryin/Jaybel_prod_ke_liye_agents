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
- Data source: `BigQuery` view `gls-training-486405.gcp_billing_data.jaybel_prod_billing_view`
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
- Memory-enabled via ADK (`PreloadMemoryTool` + after-turn memory persistence callback).

### C) Deployed Cost Agent (`vertex_agents/cost_metrics_agent`)

- BigQuery-first execution (`COST_DATA_SOURCE=bigquery`).
- LLM-first cost query pipeline:
  - context routing (structured JSON)
  - guarded SQL generation (structured JSON)
  - strict SQL validation + dry-run bytes guard
  - clarification-first execution based on structured router decisions (LLM-first semantics, thin deterministic guardrails)
  - typed response contract (`response_type=clarification|result|error`) propagated cost agent -> bridge -> frontend renderer
- BigQuery schema introspection path:
  - list columns
  - check if column exists
  - distinct values for supported scalar columns
- Memory-enabled via ADK (`PreloadMemoryTool` + after-turn memory persistence callback).

## 6) BigQuery Source & Schema Mode

Configured source:

- Project: `gls-training-486405`
- Dataset: `gcp_billing_data`
- View: `jaybel_prod_billing_view`
- Mode: `BILLING_BQ_SCHEMA_MODE=clean_view`

Optional second source (workflow / runtime view, e.g. `jaybel_prod_workflow_view`):

- Set `BQ_WORKFLOW_TABLE` and optionally `BQ_WORKFLOW_PROJECT` / `BQ_WORKFLOW_DATASET` (default to billing project/dataset). Legacy `BQ_COST_EVENTS_*` is still read as a fallback table name if `BQ_WORKFLOW_TABLE` is unset.
- When both sources are configured, the **LLM context router** chooses `gcp_billing` vs `gcp_workflow` (`bq_target`), sets confidence, and may ask a **`data_source` clarification** when the ask is ambiguous. Regex/heuristic overrides and deterministic trace totals are **off by default**; opt in only if you need legacy behavior (see env flags below). Workflow amounts are USD on top-level `cost_usd`; date filter uses `timestamp`; traces use `trace_id`.

## 7) Guardrails & Clarification Behavior

- Clarification-first for ambiguous requests (time scope, top-N, compare scope, and **data_source** when both billing and the workflow view are configured and the router is unsure).
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
- Typed handoff: when the cost tool must return clarification or error JSON, the agent
  emits a single `COST_PAYLOAD_JSON:`-prefixed block (and the orchestrator passes it
  through unchanged). The Next.js `CostResultView` parses that block after streaming completes and shows the clarification question and options as readable text (not raw JSON). The local eval script parses that prefix and applies
  `must_contain_any` to both the raw text and JSON string fields. Local Postgres
  (`DATABASE_URL` / `COST_DATA_SOURCE` fallback) remains supported but is
  **deprecated** for production; prefer BigQuery.
- Regression focus: ambiguous asks that could hit **either** BigQuery view should yield `data_source` (or router-chosen `bq_target` with high confidence) rather than silent regex routing; trace-style asks should rely on the router (and optional `usage_correlation_id`) unless `BILLING_LEGACY_REGEX_ROUTING=1`.

## 8) Key Environment Flags

- `ORCHESTRATOR_AGENT_ENGINE_RESOURCE=projects/.../reasoningEngines/...`
- `ORCHESTRATOR_LOCAL_CHAT=0` (or unset)
- `BQ_BILLING_PROJECT=gls-training-486405`
- `BQ_BILLING_DATASET=gcp_billing_data`
- `BQ_BILLING_TABLE=jaybel_prod_billing_view`
- `BILLING_BQ_SCHEMA_MODE=clean_view`
- `COST_DATA_SOURCE=bigquery`
- `BILLING_AGENT_LLM_SQL=1`
- `BILLING_CONTEXT_ROUTER_ENABLED=1`
- `BILLING_LLM_PROVIDER=auto`
- `BILLING_LLM_MAX_BYTES_BILLED=1000000000` (example)
- `BILLING_LLM_MAX_LOOKBACK_DAYS=0` (recommended in this setup)
- `BILLING_DEFAULT_TILL_NOW_SCOPE=full_history`
- `BILLING_FULL_HISTORY_START_DATE=2026-01-01`
- `BILLING_DEFAULT_PROJECT_ID=jaybel-prod` (example) — optional default GCP billing `project.id` when the user says "our project"; included in the router deployment context and applied as a thin guardrail if the model still leaves `billing_project_id` unresolved.
- `BILLING_SCHEMA_DIGEST=1` — optional: fetch a short live column digest from BigQuery (`get_table`) and pass it to the router and LLM SQL prompts (extra API calls).
- `BILLING_LEGACY_REGEX_ROUTING=1` — opt in to pre-router/post-router regex heuristics (`_heuristic_bq_target`, trace-table signals, silent trace-window resolution) that can override the router.
- `BILLING_FORCE_WORKFLOW_ON_TRACE_KEYWORDS=1` — **not implemented by default** (LLM-first). If you add it later, it would be an env-gated post-check only; today routing relies on the router prompt + optional `BILLING_LEGACY_REGEX_ROUTING`.
- `BILLING_DETERMINISTIC_TRACE_TOTAL=1` — opt in to a deterministic BigQuery sum for scalar trace totals on the workflow view (`trace_id` / `cost_usd`) instead of LLM SQL only.
- Optional: `BQ_WORKFLOW_TABLE=jaybel_prod_workflow_view` (plus `BQ_WORKFLOW_PROJECT` / `BQ_WORKFLOW_DATASET` if different from billing). Legacy: `BQ_COST_EVENTS_*` still supported as a fallback env name.
- Local smoke (orchestrator + Agent Engine): `python scripts/smoke-orchestrator-cost-questions.py` (set `ORCHESTRATOR_AUTH_DISABLED=1`, run orchestrator on `127.0.0.1:8000`, optional `ORCHESTRATOR_URL`).


- Agent Engine-only execution: implemented
- Local specialist execution path: removed
- Chat persistence + summaries: implemented
- Sidebar session history/new/delete/pagination: implemented
- Schema introspection against live BigQuery view: implemented
- Structured routing + structured SQL generation + guardrails: implemented

## 10) Agent Engine Observability Seeding (Memories + Evaluation)

To ensure Agent Engine console tabs are populated with real data:

- Memory seeding (Sessions/Traces/Memories):
  - `scripts/agent-engine-memory-smoke.py`
  - Supports multiple engine resources in one run.
  - Uses reusable multi-turn scenarios from `scripts/evals/memory_seed_cases.json`.
  - Explicitly triggers `add_session_to_memory` per seeded session.
  - Optional `--verify-memory` polls memory search and records result counts.
  - Writes run metadata to `logs/agent-engine-memory-seed-report-*.json`.
  - Console display can lag briefly after seeding; wait 30-90 seconds and refresh.

- Evaluation publishing (Evaluation tab):
  - `scripts/agent-engine-create-eval.py`
  - Uses reusable eval prompts from `scripts/evals/agent_engine_eval_cases.json`.
  - Cost-saving smoke baseline (5 critical P0 cases) is versioned in:
    - `scripts/evals/eval_smoke_p0.json`
  - Supports multi-turn regression packs (for clarification chains) via `turns` arrays in case files, e.g. `scripts/evals/agent_engine_multiturn_cases.json`.
  - Golden dataset baseline is versioned in:
    - `scripts/evals/golden_dataset_v1.json`
    - `scripts/evals/golden_dataset_schema.json`
    - `scripts/evals/golden_dataset_readme.md`
  - Performs deterministic scoring per case:
    - `expected_mode` checks (`clarify|answer|error`)
    - optional typed checks via `expected_response_type`
    - `must_contain_any` and `must_not_contain_any` checks (text plus structured fields when `COST_PAYLOAD_JSON:` is present)
  - Supports cheaper local subsets:
    - `--priority P0` (repeatable)
    - `--case-id <id>` (repeatable)
    - `--max-cases N`
  - Optional `--turn-timeout-seconds` and `--turn-retries` to stabilize long single-turn runs against the orchestrator
  - Publish mode defaults to full Vertex rubric metrics; use `--minimal-vertex-eval` to publish only `HALLUCINATION` when cost-sensitive.
  - Supports strict failure gates:
    - `--fail-on-assertion`
    - `--fail-on-priority P0`
    - `--min-pass-rate <0..1>`
  - `--publish-to-vertex --gcs-dest gs://...` creates actual evaluation runs in Vertex.
  - Writes local baseline + run metadata to `logs/agent-engine-eval-*.json`.

- One-command orchestration:
  - macOS/Linux: `scripts/seed-agent-engine-observability.sh`
  - Windows: `scripts/seed-agent-engine-observability.ps1`
  - Runs memory seeding plus four eval suites (orchestrator/cost x single/multi turn) with release-style thresholds.
  - Optional cost controls (opt-in only; defaults unchanged):
    - `SKIP_MEMORY_SMOKE=1`
    - `SKIP_VERTEX_PUBLISH=1`
    - `MINIMAL_VERTEX_EVAL=1` (publishes only `HALLUCINATION`)
  - Required env vars:
    - `ORCHESTRATOR_AGENT_ENGINE_RESOURCE`
    - `COST_AGENT_ENGINE_RESOURCE`
    - `AGENT_ENGINE_EVAL_GCS_DEST`

## 11) Online Monitoring (Cost Agent Only)

Full step-by-step narrative (telemetry, scripts, console vs API, troubleshooting): **`docs/ONLINE_MONITORING_COST_AGENT.md`**.

Goal: continuously sample live production-like traffic (from chats routed through the
deployed engines) and score the `cost_metrics_agent` in the Agent Engine console.

- Setup script:
  - macOS/Linux: `scripts/setup-agent-engine-online-monitor.sh`
  - Windows: `scripts/setup-agent-engine-online-monitor.ps1`
- Monitor defaults configured by the scripts:
  - target: `COST_AGENT_ENGINE_RESOURCE` only
  - sampling: `50%`
  - metrics: `HALLUCINATION`, `FINAL_RESPONSE_QUALITY`, `TOOL_USE_QUALITY`, `SAFETY`
  - optional run cap: `ONLINE_MONITOR_MAX_SAMPLES_PER_RUN` (default `200`)
- Pre-checks before chatting:
  - `python scripts/check-online-monitor-prereqs.py`
  - requires `ORCHESTRATOR_LOCAL_CHAT=0` and `agent_engine_chat_enabled=true` in `/health`
- Verification flow:
  1. Configure monitor once with the setup script.
  2. Chat through local Next.js UI (`/chat/stream` via local orchestrator bridge).
  3. Open GCP Console → Agent Engine → cost agent → Evaluation → Online Monitors.
  4. Confirm sampled traces and metric charts appear.
- Expected delay:
  - sampled traces usually appear within ~2-10 minutes
  - metric aggregation commonly appears within ~10-30 minutes (can take longer during backend load)
- Firestore archive of per-trace scores (Cloud Trace → Firestore): `scripts/sync-online-monitor-to-firestore.sh` — see **`docs/ONLINE_MONITORING_COST_AGENT.md` §10**.
