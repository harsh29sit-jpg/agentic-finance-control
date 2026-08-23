# Production Readiness Runbook

Everything needed to move this system from staging-grade to production,
with the switches that already exist in the codebase.

## 1. Environment hardening (backend/.env)

```env
MONGO_URL=mongodb://<mongo-host>:27017
DB_NAME=recon_control_tower
JWT_SECRET=<openssl rand -hex 32>       # rotate quarterly; rotation logs out everyone
CORS_ORIGINS=https://control.yourco.com
COOKIE_SECURE=true                      # REQUIRED behind TLS
DEMO_SEEDS=false                        # never ship seeded passwords
```

LLM key for the agent: `ANTHROPIC_API_KEY` / `OPENAI_API_KEY` /
`CUSTOM_LLM_BASE_URL` + `CUSTOM_LLM_API_KEY` + `CUSTOM_LLM_MODEL`.

## 2. Deploy (authenticated + TLS'd Mongo)

```bash
scripts/gen_mongo_tls.sh ./mongo-tls            # dev certs; use real PKI in prod
openssl rand -base64 756 > mongo-keyfile && chmod 400 mongo-keyfile

cat > .env <<ENV
JWT_SECRET=$(openssl rand -hex 32)
MONGO_ROOT_PASSWORD=...
MONGO_APP_PASSWORD=...
MONGO_BACKUP_PASSWORD=...
ANTHROPIC_API_KEY=...            # agent brain
BACKUP_S3_BUCKET=s3://your-co-recon-backups/daily
ENV

docker compose -f docker-compose.prod.yml up --build -d
```

- Mongo: auth enforced, requireTLS, single-node replica set (transactions).
- Backend connects as least-privileged `recon_app` over TLS.
- `/api/metrics` — gate at ingress or scrape internally.
- Frontend nginx terminates :8080; put real TLS at your edge.

## 3. Data operations

| Task | Mechanism |
|---|---|
| Nightly off-box backup | prod compose `backup` sidecar: dump→gzip→S3 (`BACKUP_S3_BUCKET`), 7 local retained |
| Manual backup | `python scripts/backup_db.py --gzip --s3 s3://bucket/prefix` |
| Archive batches > 90d | `python scripts/archive_batches.py --days 90` |
| Restore one batch | `python scripts/archive_batches.py --batch-id <id> --restore` |

## 4. Monitoring

- Liveness/readiness: `GET /api/health` (`status`, `database`, version)
- Metrics: `GET /api/metrics` — `http_requests_total`,
  `http_request_duration_seconds_*`, `agent_tool_calls_total`,
  `agent_state_changes_total`
- Slow requests log `SLOW ...` warnings (>1s, tunable)

## 5. Security posture & known limits (honest list)

Done: bcrypt + JWT(30m) with rotating refresh chains, per-email lockout,
RBAC on every route incl. agent actions, maker-checker on material
overrides, hash-chained auditable log w/ verifier, PII scrub before LLM,
upload fingerprints/size caps, multi-instance-safe coordination.

Still open before regulated production:
1. Mongo auth/TLS + encryption-at-rest; backups off-box
2. SSO/SCIM + MFA; refresh-token reuse detection alerts
3. Multi-instance metrics aggregation (Prometheus federation is fine as-is)
4. Scheduled connector pulls need credential vaulting
5. Load test through HTTP path at target volume

## 6. Test gates

`cd backend && python -m pytest -q` must stay green (CI enforces on every push).

## 7. Identity

- **MFA**: TOTP (RFC 6238) per operator — enrol in Admin → Two-Factor
  Authentication; 8 single-use recovery codes issued at enablement.
- **SSO**: production-grade OIDC — PKCE (S256), JWKS-verified RS256
  id_tokens with iss/aud/exp/nonce checks, signed state bundles.
  Wiring a real tenant:
  1. Register `https://<your-host>/api/auth/sso/callback` as an allowed
     redirect URI in your IdP (Auth0/Okta/Google Workspace/Entra).
  2. Set env: `OIDC_ISSUER_URL=https://<tenant>` · `OIDC_CLIENT_ID` ·
     `OIDC_CLIENT_SECRET` · `PUBLIC_BASE_URL=https://<your-host>`.
     Admins: comma list in `SSO_ADMIN_EMAILS`.
  3. Restart — the login page shows "Sign in with SSO" automatically
     (`GET /api/auth/sso/config` drives the button).
  Integration coverage: tests/test_oidc_live.py runs the full network flow
  against a live stub IdP (PKCE enforcement, nonce binding, tamper rejection).

## 8. Connector credentials & scheduled pulls

Razorpay API keys live encrypted-at-rest (AES-GCM, key derived from
JWT_SECRET) in the `vault_secrets` collection — never plaintext, never in
logs. Swap `vault.get_store()` for a HashiCorp Vault client to upgrade.

```bash
# store (controller/admin)
curl -X PUT .../api/integrations/razorpay/credentials \
  -d '{"key_id":"rzp_live_...","key_secret":"..."}'
# pull now
curl -X POST .../api/integrations/razorpay/sync -d '{"hours_back":24}'
```

Or schedule it: Admin → Batch Schedules → action `razorpay_sync`
(e.g. `30 */4 * * *` pulls every 4 hours; identical windows dedupe).

## 9. Disk hygiene (learned the hard way)

- **Rotate mongod logs.** An unrotated `--logpath` file grew to 15 GB with
  request logging and filled the disk (index builds refuse <500MB free).
  Run mongod with `--logRotate reopen` and a logrotate/cron truncation.
- WiredTiger does not return space on `drop`/`deleteMany` — schedule
  `compact` maintenance or use the archival tooling before disk pressure.
