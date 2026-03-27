#!/usr/bin/env bash
# Push DATABASE_URL (tunnel DSN) to Secret Manager for Cloud Run cost-agent.
# Prerequisites: gcloud auth, secret exists or will be created.
#
# Usage:
#   export GCP_PROJECT=your-project
#   export DATABASE_URL='postgresql://user:pass@tunnel-host:5432/postgres'
#   ./scripts/sync-db-secret.sh
#
set -euo pipefail
: "${GCP_PROJECT:?Set GCP_PROJECT}"
: "${DATABASE_URL:?Set DATABASE_URL to the tunnel DSN}"

SECRET_NAME="${DATABASE_URL_SECRET:-database-url}"

gcloud config set project "${GCP_PROJECT}" >/dev/null

if gcloud secrets describe "${SECRET_NAME}" >/dev/null 2>&1; then
  printf '%s' "${DATABASE_URL}" | gcloud secrets versions add "${SECRET_NAME}" --data-file=-
  echo "Added new version to secret: ${SECRET_NAME}"
else
  printf '%s' "${DATABASE_URL}" | gcloud secrets create "${SECRET_NAME}" --data-file=-
  echo "Created secret: ${SECRET_NAME}"
fi

echo "Grant Cloud Run access (default compute SA example):"
PROJECT_NUMBER="$(gcloud projects describe "${GCP_PROJECT}" --format='value(projectNumber)')"
echo "  gcloud secrets add-iam-policy-binding ${SECRET_NAME} \\"
echo "    --member=serviceAccount:${PROJECT_NUMBER}-compute@developer.gserviceaccount.com \\"
echo "    --role=roles/secretmanager.secretAccessor"
