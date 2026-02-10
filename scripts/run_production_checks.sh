#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
ENV_FILE="${ROOT_DIR}/.env"

if [[ ! -f "${ENV_FILE}" ]]; then
  echo "Missing .env at ${ENV_FILE}. Run scripts/bootstrap_production_env.sh first."
  exit 1
fi

# shellcheck disable=SC1090
set +B
set -a
source "${ENV_FILE}"
set +a
set -B

if [[ -z "${ALEXANDRIA_API_KEYS_JSON:-}" ]]; then
  echo "ALEXANDRIA_API_KEYS_JSON is empty"
  exit 1
fi

AGENT_KEY="$(python - <<'PY'
import json, os
items = json.loads(os.environ['ALEXANDRIA_API_KEYS_JSON'])
print(items[0]['key'])
PY
)"

HOST="127.0.0.1"
PORT="${ALEXANDRIA_CHECK_PORT:-}"
if [[ -z "${PORT}" ]]; then
  PORT="$(python - <<'PY'
import socket
s = socket.socket()
s.bind(("127.0.0.1", 0))
print(s.getsockname()[1])
s.close()
PY
)"
fi
BASE_URL="http://${HOST}:${PORT}"

# Launch API in background for live checks
PYTHON_BIN="${ROOT_DIR}/.venv/bin/python"
if [[ ! -x "${PYTHON_BIN}" ]]; then
  PYTHON_BIN="python"
fi

cleanup() {
  if [[ -n "${API_PID:-}" ]] && kill -0 "${API_PID}" >/dev/null 2>&1; then
    kill "${API_PID}" >/dev/null 2>&1 || true
    wait "${API_PID}" >/dev/null 2>&1 || true
  fi
}
trap cleanup EXIT

ALEXANDRIA_HOST=127.0.0.1 \
ALEXANDRIA_PORT="${PORT}" \
ALEXANDRIA_WORKERS=1 \
"${PYTHON_BIN}" -m alexandria --api --host 127.0.0.1 --port "${PORT}" >/tmp/alexandria_api.log 2>&1 &
API_PID=$!

healthy=0
for _ in $(seq 1 40); do
  if curl -sSf "${BASE_URL}/healthz" >/dev/null 2>&1; then
    healthy=1
    break
  fi
  sleep 0.25
done

if [[ "${healthy}" != "1" ]]; then
  echo "API failed health check at ${BASE_URL}/healthz"
  tail -n 120 /tmp/alexandria_api.log || true
  exit 1
fi

# 1) Health/readiness
curl -sSf "${BASE_URL}/healthz" | rg '"status"\s*:\s*"ok"' >/dev/null
curl -sSf "${BASE_URL}/readyz" | rg '"status"\s*:\s*"ready"' >/dev/null

# 2) Auth is enforced for reads
status_no_key="$(curl -s -o /dev/null -w '%{http_code}' "${BASE_URL}/api/stats")"
if [[ "${status_no_key}" != "401" ]]; then
  echo "Expected 401 for /api/stats without key, got ${status_no_key}"
  exit 1
fi

# 3) Authenticated reads succeed
status_with_key="$(curl -s -o /dev/null -w '%{http_code}' -H "X-API-Key: ${AGENT_KEY}" "${BASE_URL}/api/stats")"
if [[ "${status_with_key}" != "200" ]]; then
  echo "Expected 200 for /api/stats with key, got ${status_with_key}"
  exit 1
fi

# 4) A2A agent card reachable
curl -sSf "${BASE_URL}/.well-known/agent.json" | rg '"authentication"' >/dev/null

echo "Production checks passed"
