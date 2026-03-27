#!/usr/bin/env bash
# Postgres in Docker; agents + Next on host. Or use: docker compose up --build
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"
mkdir -p "$ROOT/logs"

echo ">>> Starting postgres..."
docker compose up -d postgres

for i in $(seq 1 90); do
  if docker compose exec -T postgres pg_isready -U postgres >/dev/null 2>&1; then
    break
  fi
  sleep 1
done

if ! docker compose exec -T postgres psql -U postgres -d postgres -tAc "SELECT to_regclass('public.cloud_costs');" | grep -q cloud_costs; then
  echo ">>> Applying database/schema.sql..."
  docker compose exec -T postgres psql -U postgres -d postgres < "$ROOT/database/schema.sql"
fi

export DATABASE_URL="${DATABASE_URL:-postgresql://postgres:postgres@127.0.0.1:5433/postgres}"
export COST_AGENT_CARD_URL="${COST_AGENT_CARD_URL:-http://127.0.0.1:8001/.well-known/agent.json}"
export COST_AGENT_TASKS_URL="${COST_AGENT_TASKS_URL:-http://127.0.0.1:8001/tasks/send}"

echo ">>> Starting cost-agent :8001..."
(cd "$ROOT/agents/cost_agent" && nohup python -m uvicorn main:app --host 127.0.0.1 --port 8001 \
  >"$ROOT/logs/cost-agent.log" 2>"$ROOT/logs/cost-agent.err.log" &)
sleep 2

echo ">>> Starting orchestrator :8000..."
(cd "$ROOT/agents/orchestrator" && nohup python -m uvicorn main:app --host 127.0.0.1 --port 8000 \
  >"$ROOT/logs/orchestrator.log" 2>"$ROOT/logs/orchestrator.err.log" &)
sleep 2

if [[ ! -d "$ROOT/frontend/node_modules" ]]; then
  echo ">>> npm ci..."
  (cd "$ROOT/frontend" && npm ci)
fi

echo ">>> Starting frontend :3000..."
(cd "$ROOT/frontend" && nohup npm run dev -- --hostname 127.0.0.1 --port 3000 \
  >"$ROOT/logs/frontend.log" 2>"$ROOT/logs/frontend.err.log" &)

echo ""
echo "Done. Open http://127.0.0.1:3000"
echo "Logs: $ROOT/logs/"
echo "Stop: pkill -f 'uvicorn main:app' ; pkill -f 'next dev'  (careful on shared machines)"
