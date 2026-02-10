# Production Checklist

Use this checklist before exposing Alexandria outside a trusted network.

## 1. Secrets and Configuration

- [ ] Generate `.env` using `./scripts/bootstrap_production_env.sh`
- [ ] Set `ALEXANDRIA_REQUIRE_API_KEY=true`
- [ ] Set `ALEXANDRIA_ALLOW_ANON_READ=false` (or consciously allow read-only anonymous access)
- [ ] Set strong random API keys in `ALEXANDRIA_API_KEYS_JSON`
- [ ] Set `ALEXANDRIA_TRUSTED_HOSTS` to your real hostnames
- [ ] Set `ALEXANDRIA_CORS_ORIGINS` to exact frontend origins

## 2. Network Hardening

- [ ] Run behind TLS termination (reverse proxy / ingress)
- [ ] Restrict inbound ports to only required listeners
- [ ] Monitor `429` rate-limit responses and auth failures

## 3. Runtime Health

- [ ] `GET /healthz` returns `{"status":"ok"}`
- [ ] `GET /readyz` returns `{"status":"ready"}`
- [ ] `GET /.well-known/agent.json` is reachable if A2A discovery is needed

## 4. Authorization Validation

- [ ] Confirm requests without `X-API-Key` are rejected when auth is required
- [ ] Confirm limited-scope keys cannot call protected mutation endpoints
- [ ] Confirm audit events record authenticated actor IDs (no caller ID spoofing)

## 5. Data and Backup

- [ ] Persist `data/` on durable storage
- [ ] Back up `alexandria.db` on a regular schedule
- [ ] Verify restore process from backups

## 6. Operational Baseline

- [ ] Run test suite: `pytest -q`
- [ ] Run production preflight: `./scripts/run_production_checks.sh`
- [ ] Pin and review dependencies before release
- [ ] Keep `SECURITY.md` reporting channel current
