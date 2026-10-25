#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
CONFIG_PATH="${TELCONET_TELEMETRY_CONFIG:-${ROOT_DIR}/lab/telemetry-targets.yml}"
API_URL="${TELCONET_API_URL:-http://127.0.0.1:18000}"

cd "${ROOT_DIR}"
uv run telconet-telemetry \
  --config "${CONFIG_PATH}" \
  --api-url "${API_URL}" \
  --once
