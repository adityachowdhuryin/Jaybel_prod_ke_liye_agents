#!/usr/bin/env bash
# Run a Cloudflare Tunnel using a config file (see infra/tunnel/cloudflared-postgres.example.yml).
set -euo pipefail
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT="$(cd "${SCRIPT_DIR}/.." && pwd)"
CONFIG="${CLOUDFLARED_CONFIG:-${ROOT}/infra/tunnel/cloudflared-postgres.yml}"
if [[ ! -f "${CONFIG}" ]]; then
  echo "Missing config: ${CONFIG}"
  echo "Copy infra/tunnel/cloudflared-postgres.example.yml to cloudflared-postgres.yml and edit."
  exit 1
fi
exec cloudflared tunnel --config "${CONFIG}" run
