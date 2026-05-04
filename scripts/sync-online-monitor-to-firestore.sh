#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT_DIR"

if [[ -f "config/gcp.env" ]]; then
  # shellcheck disable=SC1091
  source "config/gcp.env"
fi

if [[ ! -x "./.venv/bin/python" ]]; then
  echo "Create .venv and pip install -r requirements-adk.txt first."
  exit 1
fi

exec ./.venv/bin/python scripts/sync-online-monitor-to-firestore.py "$@"
