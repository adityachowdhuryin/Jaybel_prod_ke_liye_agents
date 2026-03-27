#!/usr/bin/env bash
#
# Phase 2 — Hybrid Cloud (GCP): build images with Cloud Build and deploy to Cloud Run.
# Compute on Cloud Run simulates hosting next to Vertex AI Agent Engine until you
# promote these services to Agent Engine; the pattern (specialist + orchestrator + UI) stays the same.
#
# Prerequisites (one-time per project):
#   - gcloud CLI authenticated:  gcloud auth login && gcloud config set project PROJECT_ID
#   - APIs:  ./scripts/enable-phase2-apis.sh   (or enable run, artifactregistry, cloudbuild, secretmanager, cloudtrace, logging, monitoring)
#   - Artifact Registry repo (Docker):
#       gcloud artifacts repositories create "${AR_REPOSITORY:-hybrid-mesh}" \
#         --repository-format=docker --location="${GCP_REGION:-us-central1}"
#   - Secret Manager: store the on-prem DB DSN (tunnel URL). Example:
#       printf '%s' 'postgresql://user:pass@tunnel-host:5432/postgres' | \
#         gcloud secrets create "${DATABASE_URL_SECRET:-database-url}" --data-file=-
#   - Cloud Trace writer (OpenTelemetry): ./scripts/grant-cloud-trace-writer.sh
#   - Grant the Cloud Run runtime service account secret accessor on that secret:
#       PROJECT_NUMBER=$(gcloud projects describe "${GCP_PROJECT}" --format='value(projectNumber)')
#       gcloud secrets add-iam-policy-binding "${DATABASE_URL_SECRET:-database-url}" \
#         --member="serviceAccount:${PROJECT_NUMBER}-compute@developer.gserviceaccount.com" \
#         --role="roles/secretmanager.secretAccessor"
#
# Usage:
#   export GCP_PROJECT="your-project-id"
#   ./deploy.sh
#
# Optional env:
#   GCP_REGION          default us-central1
#   AR_REPOSITORY       default hybrid-mesh
#   IMAGE_TAG           default latest
#   DATABASE_URL_SECRET Secret Manager id holding DATABASE_URL (default database-url)
#   CLOUD_RUN_SA        If set, both services use this SA (must have secretAccessor for cost-agent)
#
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "${SCRIPT_DIR}"

: "${GCP_PROJECT:?Set GCP_PROJECT to your Google Cloud project ID}"

REGION="${GCP_REGION:-us-central1}"
AR_REPO="${AR_REPOSITORY:-hybrid-mesh}"
TAG="${IMAGE_TAG:-latest}"
DB_SECRET="${DATABASE_URL_SECRET:-database-url}"
# Phase 2 observability: Python services export spans to Cloud Trace when ENABLE_CLOUD_TRACE=1.
PY_TRACE_ENV="GOOGLE_CLOUD_PROJECT=${GCP_PROJECT},ENABLE_CLOUD_TRACE=1"

IMAGE_ROOT="${REGION}-docker.pkg.dev/${GCP_PROJECT}/${AR_REPO}"
COST_IMAGE="${IMAGE_ROOT}/cost-agent:${TAG}"
ORCH_IMAGE="${IMAGE_ROOT}/orchestrator:${TAG}"
FRONTEND_IMAGE="${IMAGE_ROOT}/frontend:${TAG}"

gcloud config set project "${GCP_PROJECT}" >/dev/null
gcloud auth configure-docker "${REGION}-docker.pkg.dev" -q

echo ">>> [1/6] Cloud Build: cost-agent -> ${COST_IMAGE}"
gcloud builds submit "${SCRIPT_DIR}/agents/cost_agent" --tag "${COST_IMAGE}"

echo ">>> [2/6] Cloud Run: deploy cost-agent (DATABASE_URL from Secret Manager)"
COST_DEPLOY=(gcloud run deploy cost-agent
  --image "${COST_IMAGE}"
  --region "${REGION}"
  --platform managed
  --allow-unauthenticated
  --set-secrets "DATABASE_URL=${DB_SECRET}:latest"
  --set-env-vars "${PY_TRACE_ENV}"
  --memory 512Mi
  --cpu 1
  --min-instances 0
  --max-instances 10
  --timeout 300
)
if [[ -n "${CLOUD_RUN_SA:-}" ]]; then
  COST_DEPLOY+=(--service-account "${CLOUD_RUN_SA}")
fi
"${COST_DEPLOY[@]}"

COST_URL="$(gcloud run services describe cost-agent --region "${REGION}" --format='value(status.url)')"
echo ">>> Cost agent URL: ${COST_URL}"

echo ">>> [3/6] Patch cost-agent Agent Card base URL (COST_AGENT_PUBLIC_URL)"
gcloud run services update cost-agent \
  --region "${REGION}" \
  --set-env-vars "COST_AGENT_PUBLIC_URL=${COST_URL},${PY_TRACE_ENV}"

echo ">>> [4/6] Cloud Build: orchestrator -> ${ORCH_IMAGE}"
gcloud builds submit "${SCRIPT_DIR}/agents/orchestrator" --tag "${ORCH_IMAGE}"

echo ">>> [5/6] Cloud Run: deploy PA orchestrator (discovers specialist Agent Card + tasks URL)"
# CORS_ORIGINS=* keeps first deploy simple; tighten to FRONTEND_URL in step 7.
ORCH_DEPLOY=(gcloud run deploy pa-orchestrator
  --image "${ORCH_IMAGE}"
  --region "${REGION}"
  --platform managed
  --allow-unauthenticated
  --set-env-vars "COST_AGENT_CARD_URL=${COST_URL}/.well-known/agent.json,COST_AGENT_TASKS_URL=${COST_URL}/tasks/send,CORS_ORIGINS=*,${PY_TRACE_ENV}"
  --memory 512Mi
  --cpu 1
  --min-instances 0
  --max-instances 10
  --timeout 300
)
if [[ -n "${CLOUD_RUN_SA:-}" ]]; then
  ORCH_DEPLOY+=(--service-account "${CLOUD_RUN_SA}")
fi
"${ORCH_DEPLOY[@]}"

ORCH_URL="$(gcloud run services describe pa-orchestrator --region "${REGION}" --format='value(status.url)')"
echo ">>> Orchestrator URL: ${ORCH_URL}"

echo ">>> [6/6] Cloud Build + Cloud Run: frontend (NEXT_PUBLIC_ORCHESTRATOR_URL=${ORCH_URL})"
gcloud builds submit "${SCRIPT_DIR}" \
  --config "${SCRIPT_DIR}/cloudbuild-frontend.yaml" \
  --substitutions="_FRONTEND_IMAGE=${FRONTEND_IMAGE},_ORCH_URL=${ORCH_URL}"

FRONT_DEPLOY=(gcloud run deploy pa-frontend
  --image "${FRONTEND_IMAGE}"
  --region "${REGION}"
  --platform managed
  --allow-unauthenticated
  --memory 512Mi
  --cpu 1
  --min-instances 0
  --max-instances 5
  --timeout 60
)
if [[ -n "${CLOUD_RUN_SA:-}" ]]; then
  FRONT_DEPLOY+=(--service-account "${CLOUD_RUN_SA}")
fi
"${FRONT_DEPLOY[@]}"

FRONTEND_URL="$(gcloud run services describe pa-frontend --region "${REGION}" --format='value(status.url)')"
echo ">>> Frontend URL: ${FRONTEND_URL}"

echo ">>> Tighten orchestrator CORS to frontend origin (replace permissive *)"
gcloud run services update pa-orchestrator \
  --region "${REGION}" \
  --set-env-vars "COST_AGENT_CARD_URL=${COST_URL}/.well-known/agent.json,COST_AGENT_TASKS_URL=${COST_URL}/tasks/send,CORS_ORIGINS=${FRONTEND_URL},${PY_TRACE_ENV}"

echo ""
echo "Done. Open: ${FRONTEND_URL}"
echo "Health:  ${ORCH_URL}/health  |  ${COST_URL}/health"
