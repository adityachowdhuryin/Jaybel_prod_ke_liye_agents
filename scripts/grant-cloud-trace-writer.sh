#!/usr/bin/env bash
# Grant Cloud Trace agent role so Cloud Run (default or custom SA) can write spans from OpenTelemetry.
# Safe to run multiple times (idempotent binding at project level for one member).
set -euo pipefail
: "${GCP_PROJECT:?Set GCP_PROJECT}"

PROJECT_NUMBER="$(gcloud projects describe "${GCP_PROJECT}" --format='value(projectNumber)')"
SA="${CLOUD_RUN_SA:-${PROJECT_NUMBER}-compute@developer.gserviceaccount.com}"
MEMBER="serviceAccount:${SA}"

gcloud projects add-iam-policy-binding "${GCP_PROJECT}" \
  --member "${MEMBER}" \
  --role="roles/cloudtrace.agent"

echo "Granted roles/cloudtrace.agent to ${MEMBER}"
