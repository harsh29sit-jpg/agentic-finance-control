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

## 2. Deploy

```bash
JWT_SECRET=... ANTHROPIC_API_KEY=... docker compose up --build -d
```

- Put real TLS in front of the frontend nginx (or your ingress).
- `/api/metrics` is unauthenticated — scrape internally or gate at ingress.

## 3. Data operations

| Task | Command |
|---|---|
| Nightly backup (14 kept) | `30 2 * * * python scripts/backup_db.py --keep 14` |
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
