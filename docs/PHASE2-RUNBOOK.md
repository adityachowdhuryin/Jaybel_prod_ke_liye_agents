# Phase 2 runbook — hybrid cloud (GCP compute, on‑prem PostgreSQL)

This completes the operational steps from **Phase 2** in `A2A-2 agents.md`: tunnel, secrets, deploy, frontend on Cloud Run, and observability checks. Cloud Run stands in for **Vertex AI Agent Engine** until you promote the same containers to Agent Engine.

## 1. Network tunneling (on‑prem Postgres → cloud)

Pick one approach and keep the tunnel **running** whenever Cloud Run needs the database.

### Option A — ngrok (TCP)

1. Copy `infra/tunnel/ngrok-tcp.example.yml` to a local file, add your authtoken, and run:

   ```bash
   ngrok start --config ./ngrok-tcp.yml postgres
   ```

2. Note the public `host:port` ngrok prints (e.g. `4.tcp.ngrok.io:12345`).

3. Build `DATABASE_URL`:

   ```text
   postgresql://USER:PASSWORD@HOST:PORT/postgres
   ```

### Option B — Cloudflare Tunnel

1. Follow Cloudflare’s docs for **private network** or **published application** TCP to `localhost:5432`.
2. Start from a real config file (template: `infra/tunnel/cloudflared-postgres.example.yml`):

   ```bash
   export CLOUDFLARED_CONFIG=/path/to/your/cloudflared.yml
   ./scripts/run-cloudflared-tunnel.sh
   ```

3. Use the hostname and port Cloudflare exposes in `DATABASE_URL`.

**Security:** strong DB credentials, least‑privilege DB user, rotate tunnel endpoints, and prefer Zero Trust / IP restrictions where available.

## 2. Secret Manager (`DATABASE_URL`)

Push the tunnel DSN (not committed to git):

```bash
export GCP_PROJECT=your-project-id
export DATABASE_URL='postgresql://...'
./scripts/sync-db-secret.sh
```

Grant the Cloud Run runtime service account **Secret Manager Secret Accessor** on that secret (see output of the script or `deploy.sh` header).

## 3. Enable APIs and trace IAM

```bash
export GCP_PROJECT=your-project-id
./scripts/enable-phase2-apis.sh
./scripts/grant-cloud-trace-writer.sh
```

If you use a **custom** Cloud Run service account, set `CLOUD_RUN_SA` before `grant-cloud-trace-writer.sh` and pass the same value to `./deploy.sh`.

## 4. Deploy (Cloud Run)

Artifact Registry repo `hybrid-mesh` (or your `AR_REPOSITORY`) must exist in the chosen region. Then:

```bash
export GCP_PROJECT=your-project-id
./deploy.sh
```

This builds images, deploys **cost-agent**, **pa-orchestrator**, and **pa-frontend**, wires the specialist URLs and CORS, and sets `ENABLE_CLOUD_TRACE=1` / `GOOGLE_CLOUD_PROJECT` on the Python services.

## 5. Frontend on Cloud Run

Handled by `deploy.sh` (image built via `cloudbuild-frontend.yaml` with `NEXT_PUBLIC_ORCHESTRATOR_URL` pointing at the orchestrator URL).

## 6. Observability check

1. Generate traffic: open the **pa-frontend** URL, send a chat message (or `curl` the orchestrator `/health` and cost `/health`).
2. Run:

   ```bash
   export GCP_PROJECT=your-project-id
   ./scripts/verify-phase2.sh
   ```

3. In **Cloud Trace** (`Traces` list), look for services named **`cost-agent`** and **`pa-orchestrator`**. Outbound **httpx** calls from the orchestrator to the cost agent appear as child spans when trace export is working.

**LLM / Agent Engine tracing:** today’s FastAPI services use rule‑based NL→SQL with optional ADK imports. When you move inference to Vertex or Agent Engine, enable **Vertex AI / Agent Engine** tracing in the console for LLM spans; HTTP A2A spans remain visible via this app’s Cloud Trace instrumentation.

## 7. Vertex AI Agent Engine (upgrade path)

1. Keep the same container images and env pattern (`DATABASE_URL` tunnel, `COST_AGENT_*` URLs for the orchestrator).
2. Follow Google’s current **Agent Engine** / **ADK** deploy guide:  
   [Vertex AI Agent Engine](https://cloud.google.com/vertex-ai/generative-ai/docs/agent-engine/overview)
3. Map **cost-agent** → specialist runtime, **pa-orchestrator** → orchestrator runtime; inject the tunnel `DATABASE_URL` only on the workload that connects to Postgres.
4. After migration, re‑aim the frontend’s `NEXT_PUBLIC_ORCHESTRATOR_URL` at the Agent Engine–exposed HTTPS endpoint (rebuild the frontend image).

## Checklist

- [ ] Tunnel running; `DATABASE_URL` reachable from the internet path you opened  
- [ ] Secret created/updated; Cloud Run SA can access it  
- [ ] APIs enabled; `roles/cloudtrace.agent` on the runtime SA  
- [ ] `./deploy.sh` succeeded  
- [ ] `verify-phase2.sh` passes; traces visible for both Python services  

When the checklist is green, **Phase 2 is complete** for this repository’s hybrid pattern.
