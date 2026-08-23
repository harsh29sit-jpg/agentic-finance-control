# Reconciliation Control Tower

**Agentic finance-ops platform for settlement reconciliation** — deterministic-first matching across three independent ledgers, bounded AI agents on the ambiguous tail, tamper-evident audit trail, and a data-dense operator console.

Not a chat app. Not an autonomous bot. A control tower: every outcome is explicit (`matched | pending_review | exception`), evidence-backed, and human-gated.

---

## Domain Model

Three ledgers are ingested per batch and reconciled:

| Source | Ledger | Key |
|--------|--------|-----|
| **A** | PG captured payments (many per settlement) | `settlement_id` |
| **B** | Settlement ledger (net, carries UTR) — *matching authority* | `settlement_id`, `utr` |
| **C** | Bank statement credits | `utr` |

All money is **integer paise**. No floats in matching logic.

### Deterministic engine (source of truth)

- **Pass 1** — exact UTR + exact amount → auto-posted
- **Pass 2** — exact UTR + amount within policy tolerance band
- **Pass 3** — N:1 aggregation annotation (payments sum vs net, fees/TDR)
- **Timing lag** — exact amount but credit lands beyond `timing_lag_days` → `pending_review`
- **Exception taxonomy** — `MISSING_IN_BANK`, `MISSING_IN_LEDGER`, `AMOUNT_MISMATCH`, `DUPLICATE`, `TIMING_LAG`, `NARRATION_AMBIGUOUS`, `UNIDENTIFIED_CREDIT`
- Invalid rows are counted and surfaced — never silently dropped

### Agentic layer (`backend/agents/`) — propose, verify, never decide

Agents only ever operate on the exception tail, and their outputs pass through hard gates:

```
LLM provider (Anthropic / OpenAI / none)
        │  PII-scrubbed prompts, 12s timeout
        ▼
runtime.invoke_json ──► typed contract validation (pydantic)
        │                      │ fail
        │                      ▼
        │              one bounded repair round
        │                      │ still fail
        │                      ▼
        │             deterministic fallback
        ▼
evidence verifier (deterministic)  ← narration links must carry an exact
│                                    substring from real narration text that
└─ unverified ⇒ confidence=0        references the candidate's identifiers
```

| Agent | Role | Hard guarantees |
|---|---|---|
| Exception Triage | classify + suggest next step | taxonomy must be in the closed set; severity in `{low, medium, high}` |
| Narration Analysis | propose candidate links | pre-scored candidates; link survives **only** if verifier proves the evidence substring |
| Reviewer Copilot | explain failures to analysts | explanatory only |
| Settlement Q&A | read-only grounded answers | citations machine-checked against context; fabricated ones stripped |
| **Agent Orchestrator** | plan → execute → synthesize | ≤5 validated read-only tool calls per run; every citation grounded against actual tool output |

Every invocation is persisted to `model_invocations` with latency, contract acceptance, repair and verification flags. **`GET /api/agents/metrics`** aggregates per-agent accuracy over time.

**Agent Console (`/copilot`, first-class surface):** the planner selects from 9 read-only tools (`query_matches`, `query_exceptions`, `search_records`, `aggregate`, `batch_report`, `run_benchmark`, `audit_timeline`, `preview_reconcile`, `query_batches`), executes them live against reconciled data, and synthesizes grounded answers with clickable record citations and a run-trace panel. File attachments are first-class: a bank-statement CSV triggers `preview_reconcile` — an actual reconciliation of the attached statement against any batch (UTR-exact then tolerance-fuzzy), reported inline in chat. No LLM key configured? A deterministic heuristic router plans the same real tool calls, so the console stays useful offline.

No LLM key configured? Everything degrades to deterministic behavior — the pipeline is fully functional offline.

### Audit log — hash-chained

Append-only events form a SHA-256 chain (`prev_hash` → `hash`). Sequence numbers are reserved atomically; appends are serialized in-process so the chain stays linear. **`GET /api/audit/verify`** recomputes the full chain and reports any tampering or gap.

---

## Architecture

```
frontend/            React 18 (CRA+craco), Tailwind w/ Blade tokens, shadcn/ui, recharts
  src/pages/         Dashboard · Batches · Workbench · Exceptions · Reports ·
                     Evaluation · Audit · Copilot · Admin · Login
backend/
  server.py          FastAPI app: routes, middleware, audit chain, ingestion
  engine.py          normalization + 3-pass matcher + metrics + benchmark/diff math
  seed_data.py       deterministic demo ledgers + ground-truth labels
  auth.py            JWT (12h) + bcrypt + RBAC (admin/controller/compliance/analyst/support)
  models.py          pydantic request contracts
  agents/            providers · contracts · runtime · evidence · triage/narration/reviewer/copilot
  tests/             88 tests: engine units, agent guarantees, live API suites (xdist-isolated)
render.yaml           backend blueprint (Render/Railway Docker deploy) — see DEPLOY.md
```

**Security & controls:** JWT + role-gated routes (support is read-only; self-registration limited to non-privileged roles) · login/register rate limiting (10/min) · maker-checker approval for material overrides (> ₹2,000) · PAN/card scrubbing before any LLM call · upload fingerprinting for idempotent re-runs · request-ID middleware · health probe at `/api/health`.

---

## Running Locally

Prereqs: Python 3.11+, Node 18+, MongoDB on `localhost:27017`.

```bash
# 1. Backend
python3 -m venv .venv && .venv/bin/pip install -r backend/requirements.txt

cat > backend/.env <<'ENV'
MONGO_URL=mongodb://localhost:27017
DB_NAME=recon_control_tower
JWT_SECRET=<32+ random bytes>
CORS_ORIGINS=http://localhost:3000
COOKIE_SECURE=false        # http locally; true behind TLS
ENV
# optional AI: add ANTHROPIC_API_KEY=... or OPENAI_API_KEY=...

cd backend && ../.venv/bin/python -m uvicorn server:app --port 8000

# 2. Frontend (same-origin mode proxies via your dev setup; or bake the URL:)
cd frontend && npm install && npm run build && npx serve -s build
# dev mode against a split backend:
#   REACT_APP_BACKEND_URL=http://localhost:8000 npm start
```

Seeded accounts (passwords env-overridable via `ADMIN_PASSWORD` etc.):

| Role | Login |
|---|---|
| admin@recon.io | `admin123` |
| controller@recon.io | `controller123` |
| analyst@recon.io | `analyst123` |
| compliance@recon.io | `compliance123` |
| support@recon.io (read-only) | `support123` |

### First run

1. Log in as analyst → **Run demo batch** (seeded 3-ledger batch with ground truth)
2. Workbench: review matches, open the evidence drawer, approve/reject/escalate
3. Exceptions: group by taxonomy, bulk-triage, run **AI analyze** on a case
4. Controller: resolve pending approvals (maker-checker), publish stricter policy
5. Evaluation: benchmark scores vs ground truth; Audit: timeline + chain verification

## Tests

```bash
cd backend && ../.venv/bin/python -m pytest -q     # 88 tests
```

- `test_engine.py` — matching passes, tolerance boundaries, timing lag, duplicates, taxonomy coverage, benchmark/diff math, determinism
- `test_agents.py` — contracts reject bad output, bounded self-repair, **hallucinated narration links get zeroed by the verifier**, copilot citation grounding, fallback integrity
- `backend_test.py` / `test_new_features.py` — full API suites; each xdist worker gets its own isolated server + database

## API Map

```
POST /api/auth/{register,login,logout}   GET /api/auth/me
POST /api/batches/run-demo               POST /api/batches/{id}/rerun
POST /api/ingestion/upload               (CSV/JSON, idempotent by content hash)
GET  /api/dashboard/metrics              GET  /api/reconciliation[/{id}]
POST /api/reconciliation/{id}/review     GET  /api/exceptions (?group_by=taxonomy|rail|merchant_id)
POST /api/exceptions/{id}/{analyze,review,override-approval}
POST /api/exceptions/bulk-review         GET  /api/review/pending
GET  /api/reports/{batch_id}             GET  /api/benchmark[/{batch_id}]
GET  /api/diff?base&compare              GET  /api/audit   GET /api/audit/verify
GET  /api/model-invocations              GET  /api/agents/metrics
POST /api/copilot/ask                    POST /api/copilot/agent   (agentic + file attachments)
GET  /api/agents/metrics                 GET/POST /api/policies
GET  /api/health
```

## Design Language

Razorpay Blade tokens: Prussian-blue sidebar `#012652`, brand blue `#0d94fb`, white work surfaces, thin borders `#ebecf0`, 4px radii, monospace for IDs/UTRs/paise. Dark mode included. Dense tables first; charts secondary.

## One-command deploy (Docker)

```bash
JWT_SECRET=$(openssl rand -hex 32) ANTHROPIC_API_KEY=sk-ant-... docker compose up --build
# UI on http://localhost:8080 · API proxied same-origin · Mongo volume persisted
```

`docker-compose.yml` wires mongo + FastAPI + nginx-served SPA with healthchecks;
the agent needs an LLM key passed through (see `backend/.env.example`).

### Data lifecycle

Hot collections grow with every batch; keep them lean without losing history:

```bash
.venv/bin/python scripts/archive_batches.py --days 90 --dry-run  # preview
.venv/bin/python scripts/archive_batches.py --days 90            # move to archive_*
.venv/bin/python scripts/archive_batches.py --batch-id <id> --restore
```

Archived batches disappear from hot dashboards and reappear intact on restore.
