# Frontend (Next.js 14)

This UI streams assistant responses from the orchestrator SSE endpoint.

## Environment

Create `frontend/.env.local`:

```bash
NEXT_PUBLIC_ORCHESTRATOR_URL=http://127.0.0.1:8000
```

## Local Run

From `frontend/`:

```bash
npm ci
npm run dev
```

Open `http://127.0.0.1:3000`.

## Full Local Stack

From repo root (Windows PowerShell):

```powershell
.\scripts\start-all.ps1
```

This starts:
- Postgres on `5433` (host)
- Cost agent on `8001`
- Orchestrator on `8000`
- Frontend on `3000`

Stop services:

```powershell
.\scripts\stop-all.ps1
docker compose down
```
