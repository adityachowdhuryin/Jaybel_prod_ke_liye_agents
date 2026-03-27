#!/usr/bin/env bash
# Smoke-test deployed Cloud Run services and print GCP console links for observability.
set -euo pipefail
: "${GCP_PROJECT:?Set GCP_PROJECT}"

REGION="${GCP_REGION:-us-central1}"

gcloud config set project "${GCP_PROJECT}" >/dev/null

echo "=== Cloud Run services (${REGION}) ==="
gcloud run services list --region "${REGION}" --format="table(metadata.name,status.url)" || true

COST_URL="$(gcloud run services describe cost-agent --region "${REGION}" --format='value(status.url)' 2>/dev/null || true)"
ORCH_URL="$(gcloud run services describe pa-orchestrator --region "${REGION}" --format='value(status.url)' 2>/dev/null || true)"
FRONT_URL="$(gcloud run services describe pa-frontend --region "${REGION}" --format='value(status.url)' 2>/dev/null || true)"

if [[ -n "${COST_URL}" ]]; then
  echo ""
  echo "GET ${COST_URL}/health"
  curl -sfS "${COST_URL}/health" | head -c 500 || echo "(curl failed)"
fi
if [[ -n "${ORCH_URL}" ]]; then
  echo ""
  echo "GET ${ORCH_URL}/health"
  curl -sfS "${ORCH_URL}/health" | head -c 500 || echo "(curl failed)"
fi
if [[ -n "${FRONT_URL}" ]]; then
  echo ""
  echo "GET ${FRONT_URL} (expect 200)"
  curl -sfS -o /dev/null -w "%{http_code}\n" "${FRONT_URL}" || true
fi

echo ""
echo "=== Observability (open in browser) ==="
echo "Cloud Trace:  https://console.cloud.google.com/traces/list?project=${GCP_PROJECT}"
echo "Logs Explorer: https://console.cloud.google.com/logs/query?project=${GCP_PROJECT}"
echo "Cloud Run:    https://console.cloud.google.com/run?project=${GCP_PROJECT}"
echo ""
echo "Generate traffic (orchestrator chat), then Trace list should show spans for cost-agent / pa-orchestrator."
