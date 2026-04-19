#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT_DIR"

if [[ -f "config/gcp.env" ]]; then
  # shellcheck disable=SC1091
  source "config/gcp.env"
fi

PROJECT="${GOOGLE_CLOUD_PROJECT:-}"
LOCATION="${GOOGLE_CLOUD_LOCATION:-us-central1}"
ORCH_RESOURCE="${ORCHESTRATOR_AGENT_ENGINE_RESOURCE:-}"
COST_RESOURCE="${COST_AGENT_ENGINE_RESOURCE:-${1:-}}"
GCS_DEST="${AGENT_ENGINE_EVAL_GCS_DEST:-${2:-}}"

if [[ -z "$PROJECT" ]]; then
  echo "Set GOOGLE_CLOUD_PROJECT (e.g. in config/gcp.env)."
  exit 1
fi
if [[ -z "$ORCH_RESOURCE" ]]; then
  echo "Set ORCHESTRATOR_AGENT_ENGINE_RESOURCE in config/gcp.env."
  exit 1
fi
if [[ -z "$COST_RESOURCE" ]]; then
  echo "Set COST_AGENT_ENGINE_RESOURCE in config/gcp.env or pass it as first argument."
  exit 1
fi
if [[ -z "$GCS_DEST" ]]; then
  echo "Set AGENT_ENGINE_EVAL_GCS_DEST in config/gcp.env or pass gs://... as second argument."
  exit 1
fi

TIMESTAMP="$(date -u +"%Y%m%dT%H%M%SZ")"
echo "Project: $PROJECT"
echo "Location: $LOCATION"
echo "Orchestrator resource: $ORCH_RESOURCE"
echo "Cost resource: $COST_RESOURCE"
echo "Eval GCS dest: $GCS_DEST"

./.venv/bin/python "scripts/agent-engine-memory-smoke.py" \
  --project "$PROJECT" \
  --location "$LOCATION" \
  --resource "$ORCH_RESOURCE" \
  --resource "$COST_RESOURCE" \
  --scenarios "scripts/evals/memory_seed_cases.json" \
  --out "logs/agent-engine-memory-seed-report-$TIMESTAMP.json"

./.venv/bin/python "scripts/agent-engine-create-eval.py" \
  --project "$PROJECT" \
  --location "$LOCATION" \
  --resource "$ORCH_RESOURCE" \
  --cases "scripts/evals/agent_engine_eval_cases.json" \
  --publish-to-vertex \
  --gcs-dest "$GCS_DEST" \
  --display-name "orchestrator-eval-$TIMESTAMP" \
  --label "component=orchestrator" \
  --label "run_source=seed-agent-engine-observability" \
  --out "logs/agent-engine-eval-orchestrator-$TIMESTAMP.json"

./.venv/bin/python "scripts/agent-engine-create-eval.py" \
  --project "$PROJECT" \
  --location "$LOCATION" \
  --resource "$COST_RESOURCE" \
  --cases "scripts/evals/agent_engine_eval_cases.json" \
  --publish-to-vertex \
  --gcs-dest "$GCS_DEST" \
  --display-name "cost-agent-eval-$TIMESTAMP" \
  --label "component=cost_agent" \
  --label "run_source=seed-agent-engine-observability" \
  --out "logs/agent-engine-eval-cost-$TIMESTAMP.json"

echo "Observability seeding complete."
