# PRD — Razorpay AI Finance Controller (Reconciliation Control Tower)

## Original Problem Statement
Build a modern agentic finance-ops web platform in the Razorpay Blade design system: a data-dense, operator-first reconciliation control tower with bounded AI agents, deterministic financial logic, strong auditability. Ingest 3 independent ledgers (Source A payments, B settlement ledger, C bank statement), reconcile deterministic-first, use AI only on the ambiguous tail, output an honest exception ledger, and present in a Razorpay-native control interface.

## Architecture
- **Frontend:** React (CRA) + Tailwind (Blade tokens) + shadcn/ui + recharts + lucide. Pages: Login, Dashboard, Batches, Workbench, Exceptions, Reports, Audit, Copilot, Admin. Bearer-token auth in localStorage.
- **Backend:** FastAPI. Modules: `engine.py` (normalization + 3-pass deterministic engine, integer paise), `agents.py` (Claude Sonnet 4.6 via emergentintegrations — triage, narration, reviewer, copilot; deterministic fallbacks), `auth.py` (JWT + RBAC), `seed_data.py` (demo ledger generator), `server.py` (all /api routes).
- **DB:** MongoDB collections: users, batches, match_decisions, exception_cases, audit_events, model_invocations, policy_versions.
- **AI:** Claude Sonnet 4.6 (EMERGENT_LLM_KEY). Agents never mutate final match state; all invocations logged.

## User Personas & Roles (RBAC)
- **Analyst** (maker): run batches, workbench review, exception triage/resolve.
- **Controller** (checker): approve material overrides, reports, policies.
- **Compliance:** view all, audit console.
- **Admin:** full access.
- **Support:** read-only.

## Core Requirements (static)
- Deterministic 3-pass matching (exact / tolerance / aggregation), paise integers.
- Every record ends matched / pending_review / exception (no silent drops).
- Maker-checker overrides, append-only audit log, model invocation logging.
- Evidence-backed AI suggestions, human-gated.

## Implemented (2026-06)
- JWT auth + 5 seeded role accounts; RBAC-gated routes and nav.
- Batch ingestion: demo seeding + CSV/JSON upload.
- Deterministic engine: Pass 1/2/3, exception taxonomy (MISSING_IN_BANK, MISSING_IN_LEDGER, AMOUNT_MISMATCH, DUPLICATE, TIMING_LAG, UNIDENTIFIED_CREDIT), metrics.
- Reconciliation Workbench with Source A/B/C evidence drawer + pass trace + review actions.
- Exception Command Center: grouping (taxonomy/merchant/rail), value-at-risk sorting, SLA flags, AI analyze (triage + reviewer copilot + narration).
- Maker-checker override workflow + pending approvals.
- Reports: acceptance gates, value-at-risk by merchant/rail, exportable exception ledger.
- Audit console: decision timeline + model invocations.
- Finance Copilot: read-only grounded Q&A with citation cards.
- Admin: policy versioning, RBAC view, pending approvals.
- Blade theme (light/dark), monospace technical data, dense tables.
- Verified: 18/18 backend tests pass; frontend flows functional.

## Backlog (P1/P2)
- P1: table virtualization for very large batches; scheduled/async batch workers.
- P1: field masking/tokenization before AI calls (spec'd, currently no PAN in pipeline).
- P2: benchmark truth-set + shadow-mode evaluation; rerun diff view; bulk triage actions; concentration analysis charts.
- P2: split server.py into routers.

## Next Tasks
- Bulk exception triage & one-click grouped resolution.
- Scheduled batch ingestion.
- Benchmark/eval dashboard with precision & recall against a truth set.
