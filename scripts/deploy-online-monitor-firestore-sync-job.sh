#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT_DIR"

if [[ -f "config/gcp.env" ]]; then
  # shellcheck disable=SC1091
  source "config/gcp.env"
fi

PROJECT="${GOOGLE_CLOUD_PROJECT:-}"
if [[ -z "$PROJECT" ]]; then
  echo "Set GOOGLE_CLOUD_PROJECT in env or config/gcp.env."
  exit 1
fi

REGION="${ONLINE_EVAL_SYNC_RUN_REGION:-${GOOGLE_CLOUD_LOCATION:-us-central1}}"
JOB_NAME="${ONLINE_EVAL_SYNC_JOB_NAME:-online-eval-firestore-sync}"
SCHEDULER_JOB_NAME="${ONLINE_EVAL_SYNC_SCHEDULER_JOB_NAME:-online-eval-firestore-sync-daily}"
SCHEDULE="${ONLINE_EVAL_SYNC_SCHEDULE:-0 0 * * *}"
TIME_ZONE="${ONLINE_EVAL_SYNC_TIME_ZONE:-Etc/UTC}"
COLLECTION="${ONLINE_EVAL_FIRESTORE_COLLECTION:-cost_agent_online_eval_traces}"
ONLINE_EVALUATOR_RESOURCE="${ONLINE_EVALUATOR_RESOURCE:-}"
# Optional: widen scan post-filter to traces with gen_ai.agent.name (--scan-gen-ai-agent-name).
# Omit (default empty) so only traces whose spans reference the monitor are ingested (~evaluated/sampled path).
SCAN_AGENT_NAME="${ONLINE_EVAL_SYNC_SCAN_GEN_AI_AGENT_NAME:-}"
# Set to 1 only if SCAN_AGENT_NAME is set and Trace export lacks evaluator labels; ingests noisy agent stubs.
INCLUDE_AGENT_WITHOUT_EVAL_LABELS="${ONLINE_EVAL_SYNC_INCLUDE_AGENT_TRACES_WITHOUT_EVAL_LABELS:-0}"
SCAN_MAX_LIST_TRACES="${ONLINE_EVAL_SYNC_SCAN_MAX_LIST_TRACES:-3000}"
MAX_TRACES="${ONLINE_EVAL_SYNC_MAX_TRACES:-200}"
PAGE_SIZE="${ONLINE_EVAL_SYNC_PAGE_SIZE:-50}"
LOOKBACK_MINUTES="${ONLINE_EVAL_SYNC_LOOKBACK_MINUTES:-180}"
OVERLAP_MINUTES="${ONLINE_EVAL_SYNC_OVERLAP_MINUTES:-45}"
TASK_TIMEOUT="${ONLINE_EVAL_SYNC_TASK_TIMEOUT:-1800s}"
SKIP_CLOUD_BUILD="${ONLINE_EVAL_SYNC_SKIP_CLOUD_BUILD:-0}"

if [[ -z "$ONLINE_EVALUATOR_RESOURCE" ]]; then
  echo "Set ONLINE_EVALUATOR_RESOURCE in config/gcp.env or env."
  exit 1
fi

PROJECT_NUMBER="$(gcloud projects describe "$PROJECT" --format='value(projectNumber)')"
if [[ -z "$PROJECT_NUMBER" ]]; then
  echo "Could not resolve project number for $PROJECT."
  exit 1
fi

RUNTIME_SA="${ONLINE_EVAL_SYNC_RUNTIME_SA:-online-eval-sync-sa@${PROJECT}.iam.gserviceaccount.com}"
SCHEDULER_INVOKER_SA="${ONLINE_EVAL_SYNC_SCHEDULER_INVOKER_SA:-online-eval-sync-scheduler@${PROJECT}.iam.gserviceaccount.com}"
AR_REPO="${ONLINE_EVAL_SYNC_AR_REPO:-cloud-run-jobs}"
IMAGE="us-central1-docker.pkg.dev/${PROJECT}/${AR_REPO}/${JOB_NAME}:latest"

echo "Ensuring APIs are enabled..."
gcloud services enable run.googleapis.com cloudscheduler.googleapis.com cloudbuild.googleapis.com artifactregistry.googleapis.com cloudtrace.googleapis.com firestore.googleapis.com --project "$PROJECT"

echo "Ensuring Artifact Registry repository exists..."
if ! gcloud artifacts repositories describe "$AR_REPO" --location=us-central1 --project "$PROJECT" >/dev/null 2>&1; then
  gcloud artifacts repositories create "$AR_REPO" --repository-format=docker --location=us-central1 --description="Images for Cloud Run Jobs" --project "$PROJECT"
fi

echo "Ensuring service accounts exist..."
if ! gcloud iam service-accounts describe "$RUNTIME_SA" --project "$PROJECT" >/dev/null 2>&1; then
  gcloud iam service-accounts create "${RUNTIME_SA%@*}" --display-name="Online Eval Firestore Sync Runtime" --project "$PROJECT"
fi
if ! gcloud iam service-accounts describe "$SCHEDULER_INVOKER_SA" --project "$PROJECT" >/dev/null 2>&1; then
  gcloud iam service-accounts create "${SCHEDULER_INVOKER_SA%@*}" --display-name="Online Eval Firestore Sync Scheduler Invoker" --project "$PROJECT"
fi

echo "Granting runtime IAM roles..."
gcloud projects add-iam-policy-binding "$PROJECT" --member="serviceAccount:${RUNTIME_SA}" --role="roles/cloudtrace.user" >/dev/null
gcloud projects add-iam-policy-binding "$PROJECT" --member="serviceAccount:${RUNTIME_SA}" --role="roles/datastore.user" >/dev/null
gcloud projects add-iam-policy-binding "$PROJECT" --member="serviceAccount:${RUNTIME_SA}" --role="roles/logging.logWriter" >/dev/null

if [[ "${SKIP_CLOUD_BUILD}" == "1" ]]; then
  echo "Skipping Cloud Build (ONLINE_EVAL_SYNC_SKIP_CLOUD_BUILD=1); reusing image ${IMAGE}."
else
  echo "Building image with Cloud Build..."
  gcloud builds submit \
    --project "$PROJECT" \
    --config "infra/cloudrun/sync-online-monitor-firestore/cloudbuild.yaml" \
    --substitutions "_IMAGE=${IMAGE}" \
    .
fi

FILTER_DESC="post-filter: spans mention online evaluator only"
if [[ -n "${SCAN_AGENT_NAME}" ]]; then
  FILTER_DESC="post-filter: online evaluator spans OR gen_ai.agent.name=${SCAN_AGENT_NAME}"
fi

echo "Deploying Cloud Run Job ${JOB_NAME} (scan mode: no Trace list filter; ${FILTER_DESC})."
DEPLOY_ENV=( "GOOGLE_CLOUD_PROJECT=${PROJECT}" "ONLINE_EVALUATOR_RESOURCE=${ONLINE_EVALUATOR_RESOURCE}" "ONLINE_EVAL_FIRESTORE_COLLECTION=${COLLECTION}" )
if [[ -n "${SCAN_AGENT_NAME}" ]]; then
  DEPLOY_ENV+=( "ONLINE_EVAL_SCAN_GEN_AI_AGENT_NAME=${SCAN_AGENT_NAME}" )
fi
JOINED_ENV=$(IFS=,; echo "${DEPLOY_ENV[*]}")

JOB_ARGS=(
  "--project=${PROJECT}"
  "--online-evaluator=${ONLINE_EVALUATOR_RESOURCE}"
  "--collection=${COLLECTION}"
  "--scan-without-list-filter"
  "--scan-max-list-traces=${SCAN_MAX_LIST_TRACES}"
  "--max-traces=${MAX_TRACES}"
  "--page-size=${PAGE_SIZE}"
  "--lookback-minutes=${LOOKBACK_MINUTES}"
  "--overlap-minutes=${OVERLAP_MINUTES}"
)
if [[ -n "${SCAN_AGENT_NAME}" ]]; then
  JOB_ARGS+=("--scan-gen-ai-agent-name=${SCAN_AGENT_NAME}")
fi
if [[ "${INCLUDE_AGENT_WITHOUT_EVAL_LABELS}" == "1" ]]; then
  JOB_ARGS+=("--include-non-evaluated-agent-traces")
fi

JOINED_ARGS=$(IFS=,; echo "${JOB_ARGS[*]}")
gcloud run jobs deploy "$JOB_NAME" \
  --project "$PROJECT" \
  --region "$REGION" \
  --image "$IMAGE" \
  --service-account "$RUNTIME_SA" \
  --task-timeout="${TASK_TIMEOUT}" \
  --max-retries=1 \
  --set-env-vars "${JOINED_ENV}" \
  --args="${JOINED_ARGS}"

echo "Granting Scheduler invoker role on the job..."
gcloud run jobs add-iam-policy-binding "$JOB_NAME" --project "$PROJECT" --region "$REGION" --member="serviceAccount:${SCHEDULER_INVOKER_SA}" --role="roles/run.invoker" >/dev/null

RUN_URI="https://run.googleapis.com/v2/projects/${PROJECT}/locations/${REGION}/jobs/${JOB_NAME}:run"

echo "Creating/updating Cloud Scheduler job ${SCHEDULER_JOB_NAME}..."
if gcloud scheduler jobs describe "$SCHEDULER_JOB_NAME" --location "$REGION" --project "$PROJECT" >/dev/null 2>&1; then
  gcloud scheduler jobs update http "$SCHEDULER_JOB_NAME" \
    --location "$REGION" \
    --project "$PROJECT" \
    --schedule "$SCHEDULE" \
    --time-zone "$TIME_ZONE" \
    --uri "$RUN_URI" \
    --http-method POST \
    --oauth-service-account-email "$SCHEDULER_INVOKER_SA" \
    --oauth-token-scope "https://www.googleapis.com/auth/cloud-platform"
else
  gcloud scheduler jobs create http "$SCHEDULER_JOB_NAME" \
    --location "$REGION" \
    --project "$PROJECT" \
    --schedule "$SCHEDULE" \
    --time-zone "$TIME_ZONE" \
    --uri "$RUN_URI" \
    --http-method POST \
    --oauth-service-account-email "$SCHEDULER_INVOKER_SA" \
    --oauth-token-scope "https://www.googleapis.com/auth/cloud-platform"
fi

echo "Running one immediate execution to verify..."
gcloud run jobs execute "$JOB_NAME" --project "$PROJECT" --region "$REGION" --wait

echo ""
echo "Done."
echo "Cloud Run Job: ${JOB_NAME} (${REGION})"
echo "Scheduler Job: ${SCHEDULER_JOB_NAME} (${SCHEDULE} ${TIME_ZONE})"
