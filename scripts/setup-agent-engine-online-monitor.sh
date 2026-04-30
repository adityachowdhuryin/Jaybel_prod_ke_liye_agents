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
RESOURCE="${COST_AGENT_ENGINE_RESOURCE:-}"
DISPLAY_NAME="${ONLINE_MONITOR_DISPLAY_NAME:-cost-agent-online-monitor}"
SAMPLING_RATE="${ONLINE_MONITOR_SAMPLING_RATE:-50}"
MAX_SAMPLES="${ONLINE_MONITOR_MAX_SAMPLES_PER_RUN:-200}"

if [[ -z "$PROJECT" ]]; then
  echo "Set GOOGLE_CLOUD_PROJECT (e.g. in config/gcp.env)."
  exit 1
fi
if [[ -z "$RESOURCE" ]]; then
  echo "Set COST_AGENT_ENGINE_RESOURCE in config/gcp.env."
  exit 1
fi

echo "Configuring online monitor for cost agent..."
echo "  project: $PROJECT"
echo "  location: $LOCATION"
echo "  resource: $RESOURCE"
echo "  display_name: $DISPLAY_NAME"
echo "  sampling_rate: $SAMPLING_RATE%"

./.venv/bin/python "scripts/setup-agent-engine-online-monitor.py" \
  --project "$PROJECT" \
  --location "$LOCATION" \
  --resource "$RESOURCE" \
  --display-name "$DISPLAY_NAME" \
  --sampling-rate "$SAMPLING_RATE" \
  --max-evaluated-samples-per-run "$MAX_SAMPLES" \
  --metrics HALLUCINATION FINAL_RESPONSE_QUALITY TOOL_USE_QUALITY SAFETY
