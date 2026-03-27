#!/usr/bin/env bash
# Enable GCP APIs for Phase 2: Cloud Run, build, secrets, trace, logging.
set -euo pipefail
: "${GCP_PROJECT:?Set GCP_PROJECT}"

gcloud config set project "${GCP_PROJECT}" >/dev/null

gcloud services enable \
  run.googleapis.com \
  artifactregistry.googleapis.com \
  cloudbuild.googleapis.com \
  secretmanager.googleapis.com \
  cloudtrace.googleapis.com \
  logging.googleapis.com \
  monitoring.googleapis.com \
  --project "${GCP_PROJECT}"

echo "APIs enabled for ${GCP_PROJECT}."
