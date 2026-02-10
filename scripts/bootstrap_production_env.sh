#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
ENV_FILE="${ROOT_DIR}/.env"

if [[ -f "${ENV_FILE}" ]]; then
  echo ".env already exists at ${ENV_FILE}. Refusing to overwrite."
  echo "Remove it first if you want to regenerate credentials."
  exit 1
fi

random_key() {
  python - <<'PY'
import secrets
print(secrets.token_urlsafe(48))
PY
}

AGENT_KEY="$(random_key)"
HUMAN_KEY="$(random_key)"

PUBLIC_HOST="${ALEXANDRIA_PUBLIC_HOST:-localhost}"
CORS_ORIGIN="${ALEXANDRIA_CORS_ORIGIN:-https://${PUBLIC_HOST}}"

cat > "${ENV_FILE}" <<EOF_ENV
# Runtime mode
ALEXANDRIA_ENV=production

# Server
ALEXANDRIA_HOST=0.0.0.0
ALEXANDRIA_PORT=8000
ALEXANDRIA_WORKERS=2
ALEXANDRIA_LOG_LEVEL=info

# Public host for reverse proxy TLS
ALEXANDRIA_PUBLIC_HOST=${PUBLIC_HOST}

# Security and auth
ALEXANDRIA_REQUIRE_API_KEY=true
ALEXANDRIA_ALLOW_ANON_READ=false
ALEXANDRIA_API_KEYS_JSON='[{"key":"${AGENT_KEY}","actor_id":"agent-editor-1","actor_type":"agent","scopes":["*"]},{"key":"${HUMAN_KEY}","actor_id":"human-ops-1","actor_type":"human","scopes":["scrolls:write","scrolls:revise","scrolls:retract","reviews:write","replications:write","integrity:write","scholars:write"]}]'

# Network hardening
ALEXANDRIA_TRUSTED_HOSTS=${PUBLIC_HOST},localhost,127.0.0.1
ALEXANDRIA_CORS_ORIGINS=${CORS_ORIGIN}
ALEXANDRIA_MAX_REQUEST_BYTES=2000000

# Rate limiting
ALEXANDRIA_RATE_LIMIT_ENABLED=true
ALEXANDRIA_RATE_LIMIT_RPM=120
EOF_ENV

chmod 600 "${ENV_FILE}" || true

echo "Created ${ENV_FILE}"
echo "Agent API key: ${AGENT_KEY}"
echo "Human API key: ${HUMAN_KEY}"
echo
echo "Store these keys in a secure secret manager."
