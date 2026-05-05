# Online monitoring for `cost_metrics_agent` (GCP Agent Engine)

This document records **everything done** in this repository and in GCP to enable **online evaluation / online monitors** for the deployed **Vertex AI Agent Engine** `cost_metrics_agent`, including sampling, metrics, telemetry, traffic seeding, and troubleshooting.

---

## 0) Quick start

1. Ensure `config/gcp.env` has `ORCHESTRATOR_AGENT_ENGINE_RESOURCE`, `COST_AGENT_ENGINE_RESOURCE`, and `ORCHESTRATOR_LOCAL_CHAT=0`.
2. Deploy cost agent (OTEL flags are injected by `scripts/deploy-agent-engine.sh` / `.ps1`):  
   `./scripts/deploy-agent-engine.sh cost --agent-engine-id <ID>`
3. Create or verify the monitor in **GCP Console → Agent Platform → `cost_metrics_agent` → Evaluation → Online monitors** (50% sampling, four metrics). Use automation only if `onlineEvaluators` API is healthy: `bash scripts/setup-agent-engine-online-monitor.sh`
4. Run `python scripts/check-online-monitor-prereqs.py`, then chat from the UI or seed traffic.
5. See **§6** for verification and **§5** for API troubleshooting.

---

## 1) Goal

- **Scope:** `cost_metrics_agent` only (not the orchestrator engine).
- **Sampling:** **50%** of live traffic.
- **Metrics (four rubric-style scores):**
  - `HALLUCINATION`
  - `FINAL_RESPONSE_QUALITY`
  - `TOOL_USE_QUALITY`
  - `SAFETY`
- **Traffic source:** Conversations initiated from the **local Next.js UI** → **local FastAPI orchestrator** → **deployed `pa_orchestrator_agent`** → **deployed `cost_metrics_agent`** (tool / specialist path).

---

## 2) What Google’s online monitors expect (telemetry contract)

Google’s documentation for **continuous evaluation with online monitors** states that online monitors sample from **Cloud Trace** (and related signals) and score traces asynchronously. For GenAI agents, traces should include specific **OpenTelemetry** attributes and events (for example `gen_ai.*` fields and inference event payloads).

For ADK / Agent Engine workloads, Google recommends enabling:

```bash
OTEL_SEMCONV_STABILITY_OPT_IN='gen_ai_latest_experimental'
OTEL_INSTRUMENTATION_GENAI_CAPTURE_MESSAGE_CONTENT='EVENT_ONLY'
```

**Why this matters:** Without these, traces may exist but **lack the structured prompt/response content** online evaluators need to score reliably.

**Reference (Google Cloud):** [Continuous evaluation with online monitors](https://docs.cloud.google.com/gemini-enterprise-agent-platform/optimize/evaluation/evaluate-online)

---

## 3) Repository changes (automation + deploy wiring)

### 3.1 Deploy scripts — inject OTEL prompt-capture into cost agent `.env`

**Files changed:**

| File | Change |
|------|--------|
| `scripts/deploy-agent-engine.sh` | When deploying **cost**, the generated `vertex_agents/cost_metrics_agent/.env` now includes `OTEL_SEMCONV_STABILITY_OPT_IN=gen_ai_latest_experimental` and `OTEL_INSTRUMENTATION_GENAI_CAPTURE_MESSAGE_CONTENT=EVENT_ONLY`. |
| `scripts/deploy-agent-engine.ps1` | Same two variables added to the cost-agent `.env` lines on Windows deploys. |

**Existing behavior preserved:** Both scripts still pass `--trace_to_cloud --otel_to_cloud` to `adk deploy agent_engine`, which exports telemetry to Google Cloud.

### 3.2 Online monitor setup scripts (API automation — when backend is healthy)

**Files added:**

| File | Purpose |
|------|---------|
| `scripts/setup-agent-engine-online-monitor.py` | Creates or updates a single **online evaluator** for `COST_AGENT_ENGINE_RESOURCE` with configurable sampling and the four metrics. Uses Vertex **v1beta1** `onlineEvaluators` REST API. Includes a guard so the target resource must match `COST_AGENT_ENGINE_RESOURCE` unless `--allow-non-cost-resource` is passed. |
| `scripts/setup-agent-engine-online-monitor.sh` | Loads `config/gcp.env`, runs the Python script with defaults: 50% sampling, four metrics, display name `cost-agent-online-monitor` (override via `ONLINE_MONITOR_DISPLAY_NAME`). |
| `scripts/setup-agent-engine-online-monitor.ps1` | Windows equivalent wrapper. |

**Optional environment variables (documented in `config/gcp.env.example`):**

- `ONLINE_MONITOR_DISPLAY_NAME` — default `cost-agent-online-monitor`
- `ONLINE_MONITOR_SAMPLING_RATE` — default `50` (integer percent)
- `ONLINE_MONITOR_MAX_SAMPLES_PER_RUN` — default `200` (cost cap per evaluation loop)

### 3.3 Prerequisite checker (local path → engines)

**File added:** `scripts/check-online-monitor-prereqs.py`

Checks:

- `ORCHESTRATOR_LOCAL_CHAT` is not forcing local-only chat (`1` / `true` / `yes`).
- `ORCHESTRATOR_AGENT_ENGINE_RESOURCE` is set.
- `COST_AGENT_ENGINE_RESOURCE` is set.
- `http://127.0.0.1:8000/health` is reachable and reports `agent_engine_chat_enabled: true`.

### 3.4 Multi-turn smoke eval dataset (optional, for cost testing)

**File added:** `scripts/evals/eval_smoke_p0_multiturn.json`

Five multi-turn cases (3× two-turn, 2× three-turn) for exercising clarification flows through the cost agent in eval harnesses — **not required** for online monitors, but useful for regression traffic.

### 3.5 Documentation updates

**Files updated:**

| File | Content added |
|------|----------------|
| `A2A-2 agents.md` | New section **“Online Monitoring (Cost Agent Only)”**: setup commands, env vars, verification flow, expected delays, troubleshooting pointers. |
| `vertex_agents/AGENT-ENGINE-DEPLOY.txt` | Post-deploy notes for online monitors, wrappers, and metric list. |
| `config/gcp.env.example` | Commented optional `ONLINE_MONITOR_*` variables. |

---

## 4) What was run operationally (this project / this workspace)

### 4.1 Local stack

- `bash scripts/start-all.sh` — starts Postgres, orchestrator on `:8000`, Next.js on `:3000`, and writes `frontend/.env.development.local`.

### 4.2 Prerequisite verification

- `python scripts/check-online-monitor-prereqs.py` — confirmed engine chat path enabled when orchestrator is up.

### 4.3 Redeploy cost agent with new OTEL variables

After updating deploy scripts, **cost** was redeployed in-place to the existing reasoning engine:

- Resource pattern: `projects/<project>/locations/<region>/reasoningEngines/<numeric_id>`
- Example used in this workspace: engine id `8670793226862460928` under project `gls-training-486405`, region `us-central1`.

This pushed the new `.env` (including OTEL flags) into the Agent Engine deployment bundle via ADK deploy.

### 4.4 Programmatic “UI-equivalent” traffic seeding

To generate **live** traffic without manual clicking, HTTP `POST` requests were sent to:

- `http://127.0.0.1:8000/chat/stream`

with JSON body:

```json
{
  "message": "<prompt>",
  "session_id": "<stable session string>",
  "client_message_id": "<unique idempotency key>"
}
```

**Sessions used (examples from this work):**

- `online-monitor-seed-1777541455` — first 10-question batch.
- `online-monitor-seed2-1777543944` — second 10-question batch.

Each request read a bounded prefix of the SSE stream so the orchestrator would execute the full Agent Engine path for that turn.

### 4.5 Additional cost-engine traffic (eval harness)

- Ran `scripts/agent-engine-create-eval.py` against `COST_AGENT_ENGINE_RESOURCE` with a small case subset to produce another trace-bearing interaction (baseline JSON under `logs/online-monitor-traffic-seed-*.json`).

---

## 5) GCP Console work (manual — required when API was failing)

### 5.1 API instability for `onlineEvaluators` (programmatic create)

During this work, repeated calls to:

`https://aiplatform.googleapis.com/v1beta1/projects/<project>/locations/<region>/onlineEvaluators`

returned **500 Internal Server Error** or **503 Unavailable** even with valid OAuth credentials (after `401` was ruled out by using a bearer token from Application Default Credentials).

**Implication:** Automated creation via `scripts/setup-agent-engine-online-monitor.py` could not complete reliably until the backend endpoint recovers.

### 5.2 Manual monitor creation in Console (what you did)

You created the online monitor directly in:

**GCP Console → Agent Platform → Agents → `cost_metrics_agent` → Evaluation → Online monitors → New monitor**

Your monitor shows:

- **Sampling:** 50%
- **Status:** Active
- Example monitor names seen in screenshots: `Monitor_3495074785816215552`, later `Monitor_4116571534393344000` (IDs are assigned by GCP).

That satisfies the **“one monitor on cost agent, 50%, four metrics”** intent from the product side even when the REST automation path was flaky.

**Action for you:** Open the monitor configuration in the console and confirm all **four** metrics are selected. If any are missing, edit the monitor and add them.

---

## 6) How to verify it is working

1. **Prereqs (local):**  
   `python scripts/check-online-monitor-prereqs.py`

2. **Generate traffic:**  
   Use the Next.js UI or repeat the `/chat/stream` seeding pattern.

3. **In GCP Console:**  
   `cost_metrics_agent` → **Evaluation** → **Online monitors** → **View traces**  
   - You should see sampled traces after a few minutes (sampling is probabilistic; send enough turns).

4. **Scores / charts:**  
   Online monitors run on a scheduled loop (Google documents roughly **~10 minutes** cadence). Expect:
   - **First signals:** often a few minutes  
   - **Stable charts:** commonly **10–30 minutes** (longer during platform load)

5. **If nothing appears:**  
   Use Logs Explorer filters documented by Google for online evaluator diagnostics (monitor resource labels, trace id, reasoning engine id).  
   See troubleshooting in: [Continuous evaluation with online monitors](https://docs.cloud.google.com/gemini-enterprise-agent-platform/optimize/evaluation/evaluate-online)

---

## 7) Commands reference (copy/paste)

### Prereqs

```bash
source .venv/bin/activate
set -a && source config/gcp.env && set +a
python scripts/check-online-monitor-prereqs.py
```

### Redeploy cost agent (includes OTEL flags via deploy scripts)

```bash
source .venv/bin/activate
set -a && source config/gcp.env && set +a
./scripts/deploy-agent-engine.sh cost \
  --project "$GOOGLE_CLOUD_PROJECT" \
  --region "$GOOGLE_CLOUD_LOCATION" \
  --agent-engine-id <YOUR_COST_ENGINE_NUMERIC_ID>
```

### Automated monitor setup (retry when API is healthy)

```bash
source .venv/bin/activate
set -a && source config/gcp.env && set +a
python scripts/setup-agent-engine-online-monitor.py \
  --resource "$COST_AGENT_ENGINE_RESOURCE" \
  --sampling-rate 50 \
  --metrics HALLUCINATION FINAL_RESPONSE_QUALITY TOOL_USE_QUALITY SAFETY
```

Or wrapper:

```bash
bash scripts/setup-agent-engine-online-monitor.sh
```

---

## 8) Git history (what landed on GitHub)

Relevant commits pushed to `main` on `https://github.com/adityachowdhuryin/Orchestrator-and-Cost-agent` include:

- **This guide (full narrative):** `8adcd8d` — `docs: add online monitoring guide for cost_metrics_agent`
- **Online monitor tooling + partial docs + OTEL deploy wiring:** `231d56c` — `feat(observability): add cost-agent online monitor setup tooling`

Earlier related work (eval cost tiers, etc.) remains in history as separate commits.

---

## 9) Summary checklist

| Item | Status |
|------|--------|
| Cost agent deploy includes OTEL gen_ai semconv + message capture | Done in deploy scripts + redeployed |
| Local UI path hits Agent Engine (`agent_engine_chat_enabled`) | Verified via prereq script when stack is up |
| Online monitor exists in GCP for `cost_metrics_agent` | Done manually in Console (API automation blocked intermittently) |
| Sampling 50% | Per your monitor + script defaults |
| Four metrics | Confirm in monitor UI; script defaults match when API works |
| Traffic seeded for sampling | Done via `/chat/stream` batches |

---

## 10) Persist scores in Firestore (Cloud Trace source)

Online monitor rubric scores are attached to **Cloud Trace** spans (not `evaluationRuns`). The repo includes a poller that lists traces for your monitor, parses metric labels on spans, and **upserts** one Firestore document per `trace_id`.

**Scripts**

| Script | Purpose |
|--------|--------|
| `scripts/sync-online-monitor-to-firestore.py` | Main logic: Trace API `list` + `COMPLETE` view, label parsing, Firestore writes. |
| `scripts/sync-online-monitor-to-firestore.sh` | Loads `config/gcp.env`, runs Python from `.venv`. |
| `scripts/sync-online-monitor-to-firestore.ps1` | Windows equivalent. |

**Prerequisite:** the project must have a **Firestore Native** database (default `(default)` is fine). If reads fail with “database does not exist”, create one, for example:

`gcloud firestore databases create --database='(default)' --location=us-central1 --type=firestore-native --project=YOUR_PROJECT_ID`

**Configuration (env or flags)**

- `ONLINE_EVALUATOR_RESOURCE` — full name, e.g. `projects/…/locations/us-central1/onlineEvaluators/4116571534393344000` (same string you use in Console → Traces).
- Default Trace **list filter** is `+online_evaluator:"<full resource>"`, aligned with Cloud Logging’s `resource.labels.online_evaluator` troubleshooting field. If **list returns zero traces**, either set `ONLINE_EVAL_TRACE_FILTER` to the exact filter string from Trace Explorer / API (see `--dump-labels-trace-id` below), or use **`--scan-without-list-filter`**: list traces **without** a server-side filter (within the time window), then keep only traces whose span labels **mention** that evaluator resource (post-filter; caps with `--scan-max-list-traces`).
- `ONLINE_EVAL_FIRESTORE_COLLECTION` (default `cost_agent_online_eval_traces`) — one document per trace, **document id = trace_id** (merge upsert).
- Cursor doc: collection `online_eval_firestore_sync`, document `cost_agent_cursor` — stores `last_window_end` for incremental polling with overlap (`--overlap-minutes`, default 45).
- Optional: `FIRESTORE_DATABASE_ID` for non-default Firestore database; `ONLINE_EVAL_METRIC_NAMES` to override the four default metric names.

**IAM**

- `cloudtrace.traces.list` and `cloudtrace.traces.get` (e.g. `roles/cloudtrace.user`).
- Firestore write (e.g. `roles/datastore.user` on the project).

**Debug**

```bash
python scripts/sync-online-monitor-to-firestore.py --project YOUR_PROJECT \
  --dump-labels-trace-id TRACE_ID_HEX32
```

Prints full trace JSON (truncated) and every span label so you can tune `ONLINE_EVAL_TRACE_FILTER` or metric key heuristics.

**Example run**

```bash
export ONLINE_EVALUATOR_RESOURCE='projects/gls-training-486405/locations/us-central1/onlineEvaluators/4116571534393344000'
bash scripts/sync-online-monitor-to-firestore.sh --max-traces 50
```

Schedule with Cloud Scheduler → Cloud Run Job or cron; keep overlap so late-arriving traces are re-listed idempotently.

**Time-range backfill (April 30-style)** — ignores the Firestore cursor for that run:

```bash
bash scripts/sync-online-monitor-to-firestore.sh \
  --online-evaluator 'projects/PROJECT/locations/REGION/onlineEvaluators/MONITOR_ID' \
  --start-time '2026-04-30T00:00:00Z' \
  --end-time '2026-05-01T00:00:00Z' \
  --scan-without-list-filter \
  --scan-gen-ai-agent-name cost_metrics_agent \
  --scan-max-list-traces 5000 \
  --max-traces 200
```

Optional: `--trace-ids 'id1,id2'` (or `--trace-ids-file PATH` with one hex id per line) fetches those trace IDs with `GET` and upserts Firestore (then exits). Add `--update-cursor-after-backfill` if you want to move the incremental cursor after a backfill.

**Firestore only for “evaluated” traces (match the Console Traces sidebar filter):**

1. **List crawl with an ID allowlist** — Paste the trace IDs from Agent Platform (online monitor filter on), one per line, into a file and run with  
   `--evaluated-trace-allowlist-file PATH`. Only those IDs are upserted from the Cloud Trace `list` results in the time window.
2. **Drop gen_ai-only noise** — With `--scan-without-list-filter` + `--scan-gen-ai-agent-name`, the default is to **skip** traces that have no online-evaluator span label and no rubric labels (unless they appear in `--metrics-overrides`). Use `--include-non-evaluated-agent-traces` only if you want the old broader behavior.
3. **Prune Firestore** — After tightening ingest, remove documents that are not on your golden list:  
   `--prune-firestore-except-allowlist-file PATH` (use `--dry-run` first to print ids that would be deleted). That command deletes and exits (it does not run a sync in the same invocation).
4. **Metrics on every stored trace** — Add each `trace_id` to your `--metrics-overrides` JSON (from the Evaluation tab) and run `--apply-metrics-overrides-only` or merge during sync.

**Why `metrics` can be `{}`:** The **Agent Platform / Console Evaluation** tab loads scores from an internal path. **`cloudtrace.googleapis.com` v1 trace JSON** for the same `traceId` typically has **no** `HALLUCINATION` / `SAFETY` / etc. span labels, and **Cloud Monitoring’s** `aiplatform.googleapis.com/online_evaluator/scores` series is **aggregated** (no `trace_id` label), so this repo cannot infer per-trace numbers from those APIs alone.

**Fill `metrics` in Firestore:** export from Console (Evaluation tab) and use a **JSON overrides** file + patch:

```bash
# Build a file like scripts/online-eval-metrics-overrides.example.json (one key per trace_id)

bash scripts/sync-online-monitor-to-firestore.sh \
  --metrics-overrides "path/to/overrides.json" \
  --apply-metrics-overrides-only
```

During a normal sync, pass **`--metrics-overrides`** to merge the same file whenever each trace is written.

---

## 11) Optional next steps

- When `onlineEvaluators` API is stable again, run `scripts/setup-agent-engine-online-monitor.sh` once to align **display name / caps** with repo defaults and avoid drift vs console-only config.
- If you use multimodal payloads, consider GCS upload hooks for OTEL (see Google doc section on multimodal recording).

---

*Document generated to capture implementation and operational steps for this repository. For authoritative Google behavior and UI changes, always refer to current Cloud documentation.*
