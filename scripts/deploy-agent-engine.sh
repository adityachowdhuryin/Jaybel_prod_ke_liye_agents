#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
ADK="$ROOT/.venv/bin/adk"

usage() {
  cat <<'EOF'
Usage:
  scripts/deploy-agent-engine.sh cost|orchestrator [options]

Options:
  --project <id>                     (default: gls-training-486405)
  --region <id>                      (default: us-central1)
  --agent-engine-id <id>             update existing engine id
  --force-new-engine                 create a fresh engine (no --agent_engine_id)
  --cost-agent-engine-id <id>        orchestrator target cost engine id
  --cost-agent-query-endpoint <url>  explicit orchestrator specialist endpoint

Cost-only options:
  --cost-data-source auto|bigquery|postgres     (default: bigquery)
  --billing-schema-mode raw_export|clean_view   (default: clean_view)
  --billing-project <id>                        (default: --project)
  --billing-dataset <id>                        (default: gcp_billing_data)
  --billing-table <id>                          (default: clean_billing_view)
  --billing-default-till-now-scope full_history|month_to_date (default: full_history)
  --billing-full-history-start-date YYYY-MM-DD  (default: 2026-01-01)
  --database-url <dsn>                          optional Postgres fallback DSN
EOF
}

if [[ $# -lt 1 ]]; then
  usage
  exit 1
fi

AGENT="$1"
shift
if [[ "$AGENT" != "cost" && "$AGENT" != "orchestrator" ]]; then
  usage
  exit 1
fi

PROJECT="gls-training-486405"
REGION="us-central1"
AGENT_ENGINE_ID=""
FORCE_NEW_ENGINE=0
COST_AGENT_ENGINE_ID="5616525846761177088"
COST_AGENT_QUERY_ENDPOINT=""

COST_DATA_SOURCE="bigquery"
BILLING_SCHEMA_MODE="clean_view"
BILLING_PROJECT=""
BILLING_DATASET="gcp_billing_data"
BILLING_TABLE="clean_billing_view"
BILLING_DEFAULT_TILL_NOW_SCOPE="full_history"
BILLING_FULL_HISTORY_START_DATE="2026-01-01"
DATABASE_URL="${DATABASE_URL:-}"

while [[ $# -gt 0 ]]; do
  case "$1" in
    --project) PROJECT="$2"; shift 2;;
    --region) REGION="$2"; shift 2;;
    --agent-engine-id) AGENT_ENGINE_ID="$2"; shift 2;;
    --force-new-engine) FORCE_NEW_ENGINE=1; shift;;
    --cost-agent-engine-id) COST_AGENT_ENGINE_ID="$2"; shift 2;;
    --cost-agent-query-endpoint) COST_AGENT_QUERY_ENDPOINT="$2"; shift 2;;
    --cost-data-source) COST_DATA_SOURCE="$2"; shift 2;;
    --billing-schema-mode) BILLING_SCHEMA_MODE="$2"; shift 2;;
    --billing-project) BILLING_PROJECT="$2"; shift 2;;
    --billing-dataset) BILLING_DATASET="$2"; shift 2;;
    --billing-table) BILLING_TABLE="$2"; shift 2;;
    --billing-default-till-now-scope) BILLING_DEFAULT_TILL_NOW_SCOPE="$2"; shift 2;;
    --billing-full-history-start-date) BILLING_FULL_HISTORY_START_DATE="$2"; shift 2;;
    --database-url) DATABASE_URL="$2"; shift 2;;
    -h|--help) usage; exit 0;;
    *) echo "Unknown option: $1"; usage; exit 1;;
  esac
done

if [[ ! -x "$ADK" ]]; then
  echo "ADK not found at $ADK. Create .venv and install requirements-adk.txt first."
  exit 1
fi

if [[ "$AGENT" == "cost" ]]; then
  AGENT_DIR="$ROOT/vertex_agents/cost_metrics_agent"
else
  AGENT_DIR="$ROOT/vertex_agents/pa_orchestrator_agent"
fi

if [[ -z "$AGENT_ENGINE_ID" && "$FORCE_NEW_ENGINE" -eq 0 ]]; then
  if [[ "$AGENT" == "cost" ]]; then
    AGENT_ENGINE_ID="$COST_AGENT_ENGINE_ID"
  fi
fi

if [[ "$AGENT" == "cost" ]]; then
  [[ -z "$BILLING_PROJECT" ]] && BILLING_PROJECT="$PROJECT"
  cat > "$AGENT_DIR/.env" <<EOF
GOOGLE_CLOUD_PROJECT=$PROJECT
GOOGLE_CLOUD_LOCATION=$REGION
COST_DATA_SOURCE=$COST_DATA_SOURCE
BQ_BILLING_PROJECT=$BILLING_PROJECT
BQ_BILLING_DATASET=$BILLING_DATASET
BQ_BILLING_TABLE=$BILLING_TABLE
BILLING_BQ_SCHEMA_MODE=$BILLING_SCHEMA_MODE
BILLING_DEFAULT_TILL_NOW_SCOPE=$BILLING_DEFAULT_TILL_NOW_SCOPE
BILLING_FULL_HISTORY_START_DATE=$BILLING_FULL_HISTORY_START_DATE
EOF
  if [[ -n "$DATABASE_URL" ]]; then
    echo "DATABASE_URL=$DATABASE_URL" >> "$AGENT_DIR/.env"
  fi
  echo "Wrote $AGENT_DIR/.env for BigQuery-first deploy"
else
  if [[ -z "$COST_AGENT_QUERY_ENDPOINT" ]]; then
    COST_AGENT_QUERY_ENDPOINT="https://$REGION-aiplatform.googleapis.com/v1/projects/$PROJECT/locations/$REGION/reasoningEngines/${COST_AGENT_ENGINE_ID}:query"
  fi
  cat > "$AGENT_DIR/.env" <<EOF
GOOGLE_CLOUD_PROJECT=$PROJECT
GOOGLE_CLOUD_LOCATION=$REGION
COST_AGENT_QUERY_ENDPOINT=$COST_AGENT_QUERY_ENDPOINT
EOF
  echo "Wrote $AGENT_DIR/.env with COST_AGENT_QUERY_ENDPOINT"
fi

CMD=("$ADK" deploy agent_engine --project "$PROJECT" --region "$REGION" --trace_to_cloud --otel_to_cloud)
if [[ -n "$AGENT_ENGINE_ID" && "$FORCE_NEW_ENGINE" -eq 0 ]]; then
  CMD+=(--agent_engine_id "$AGENT_ENGINE_ID")
fi
CMD+=("$AGENT_DIR")

echo "Running: ${CMD[*]}"
"${CMD[@]}"

echo ""
echo "Deploy finished."
echo "If you created a new engine, copy its resource identity and update:"
echo "  - ORCHESTRATOR_AGENT_ENGINE_RESOURCE in config/gcp.env (local UI -> engine chat)"
echo "  - COST_AGENT_QUERY_ENDPOINT/COST_AGENT_ENGINE_RESOURCE for orchestrator agent deploy env"
