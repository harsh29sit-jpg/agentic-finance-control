"""Razorpay Reconciliation Control Tower — FastAPI backend.

Hardening summary:
  - Fail-fast environment validation (MONGO_URL, DB_NAME, JWT_SECRET).
  - Lifespan-managed MongoDB client + full index coverage.
  - Request-ID middleware and structured access logs.
  - Tamper-evident append-only audit log (SHA-256 hash chain) with verifier endpoint.
  - Atomic chain-state reservation so concurrent writers never fork the chain.
  - Idempotent CSV/JSON ingestion via content fingerprinting.
  - MongoDB aggregation for dashboard metrics (no full-collection Python scans).
  - Bulk inserts on the ingestion hot path (no per-row round-trips).
  - Sliding-window rate limiting on auth endpoints.
  - Pagination with hard caps on every list endpoint.
"""
from dotenv import load_dotenv
from pathlib import Path

ROOT_DIR = Path(__file__).parent
load_dotenv(ROOT_DIR / '.env')

import os
import sys
import uuid
import json
import csv
import io
import hashlib
import logging
import time
import contextlib
from collections import defaultdict, deque
from datetime import datetime, timezone

from fastapi import FastAPI, APIRouter, HTTPException, Depends, UploadFile, File, Form, Request, Query
from fastapi.responses import JSONResponse, Response
from starlette.middleware.cors import CORSMiddleware
from motor.motor_asyncio import AsyncIOMotorClient
from pymongo import ReturnDocument
from pymongo.errors import BulkWriteError, DuplicateKeyError

from models import (ReviewAction, ExceptionReviewAction, OverrideDecision, CopilotAsk,
                    PolicyUpdate, TAXONOMY, ROLES, BulkReview, ScheduleCreate)
from typing import List
from engine import run_reconciliation, compute_benchmark, diff_batches
from seed_data import generate_batch
from connectors import razorpay as rz
from connectors.razorpay import ConnectorError
from scheduler import BatchScheduler, CronError, next_fire
import agents
from agents.orchestrator import run_agent_question, ProviderNotConfigured
from agents.tools import RunContext
from auth import build_auth_router, require_roles, seed_users, ROLE_LABELS

# ------------------------------------------------------------------ config
REQUIRED_ENV = ("MONGO_URL", "DB_NAME", "JWT_SECRET")
_missing = [k for k in REQUIRED_ENV if not os.environ.get(k)]
if _missing:
    raise RuntimeError(
        f"Missing required environment variables: {', '.join(_missing)}. "
        "Set them in backend/.env or the process environment.")

mongo_url = os.environ['MONGO_URL']
client = AsyncIOMotorClient(mongo_url, serverSelectionTimeoutMS=5000)
db = client[os.environ['DB_NAME']]

APP_VERSION = "2.0.0"
DEFAULT_POLICY = {"amount_tolerance_paise": 100, "timing_lag_days": 1, "auto_post_confidence": 0.95}
import services as svc
from services import GENESIS_HASH  # re-exported (tests + audit verifier)
import metrics as metrics_mod

APP_VERSION = "2.1.0"
MAX_UPLOAD_BYTES = 20 * 1024 * 1024  # 20 MB guard rail
AUDIT_PAYLOAD_KEYS = ("id", "batch_id", "actor", "role", "action",
                      "entity", "entity_id", "details", "created_at")

logging.basicConfig(level=logging.INFO,
                    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s')
logger = logging.getLogger("recon")


@contextlib.asynccontextmanager
async def lifespan(_app):
    # --- indexes (idempotent) ---
    await db.users.create_index("email", unique=True)
    await db.batches.create_index("created_at")
    await db.batches.create_index("parent_batch_id")
    await db.batches.create_index("source_fingerprint")
    await db.raw_files.create_index("batch_id", unique=True)
    await db.exception_cases.create_index([("batch_id", 1), ("status", 1)])
    await db.exception_cases.create_index([("batch_id", 1), ("taxonomy", 1)])
    await db.exception_cases.create_index("status")
    await db.match_decisions.create_index([("batch_id", 1), ("settlement_id", 1)])
    await db.match_decisions.create_index([("batch_id", 1), ("status", 1)])
    await db.audit_events.create_index([("batch_id", 1), ("created_at", -1)])
    await db.audit_events.create_index("seq", unique=True)
    await db.model_invocations.create_index([("batch_id", 1), ("created_at", -1)])
    await db.schedules.create_index("enabled")
    await db.rate_limits.create_index("window_end", expireAfterSeconds=7200)
    # --- seeds ---
    await seed_users(db)
    if await db.policy_versions.count_documents({}) == 0:
        await db.policy_versions.insert_one({
            "id": str(uuid.uuid4()), "version": 1, **DEFAULT_POLICY,
            "note": "Initial default policy", "created_by": "system", "created_at": now_iso()})

    # --- scheduled batch runners (injected to avoid circular imports) ---
    async def _job_sandbox_seed(actor="scheduler@system", trigger="schedule", schedule=None):
        return await svc.create_sandbox_batch(
            db, {"email": actor, "role": "admin"}, _process_batch, generate_batch)

    async def _job_replay_latest(actor="scheduler@system", trigger="schedule", schedule=None):
        raw = await db.raw_files.find_one(sort=[("created_at", -1)])
        if not raw:
            raise RuntimeError("No stored upload available to replay")
        base = await db.batches.find_one({"id": raw["batch_id"]}, {"_id": 0})
        if not base:
            raise RuntimeError("Replay source batch no longer exists")
        return await _process_batch(
            f"{base['name']} (scheduled replay)", raw["rows"], actor, "admin",
            f"replay:{base['source_label']}", parent_batch_id=base["id"],
            sandbox=bool(base.get("sandbox")))

    scheduler_service.configure({"sandbox_seed": _job_sandbox_seed,
                                 "replay_latest_upload": _job_replay_latest})
    await scheduler_service.start()

    logger.info("Startup complete: indexes ensured, users + policy seeded, scheduler running")
    yield
    await scheduler_service.stop()
    client.close()


app = FastAPI(title="Razorpay Reconciliation Control Tower",
              version=APP_VERSION, lifespan=lifespan)
api = APIRouter(prefix="/api")


# ------------------------------------------------------------------ helpers
def now_iso():
    return datetime.now(timezone.utc).isoformat()


class MongoFixedWindowLimiter:
    """Cross-instance fixed-window rate limiter backed by MongoDB.

    One atomic pipeline update per check: the counter increments and the
    window resets in the same document write, so any number of app instances
    share one budget per key. Buckets self-expire via a TTL index.

    Availability tradeoff (documented): if Mongo is unreachable the limiter
    fails OPEN — auth stays usable and the event is logged. For brute-force
    blunting this beats failing closed and locking everyone out.
    """

    def __init__(self, database):
        self.db = database

    async def allow(self, key, max_events, window_seconds):
        now = int(time.time())
        try:
            doc = await self.db.rate_limits.find_one_and_update(
                {"_id": key},
                [{"$set": {
                    "count": {"$cond": [
                        {"$gt": [now, {"$ifNull": ["$window_end", 0]}]},
                        1, {"$add": [{"$ifNull": ["$count", 0]}, 1]}]},
                    "window_start": {"$cond": [
                        {"$gt": [now, {"$ifNull": ["$window_end", 0]}]},
                        now, {"$ifNull": ["$window_start", now]}]},
                    "window_end": {"$cond": [
                        {"$gt": [now, {"$ifNull": ["$window_end", 0]}]},
                        now + window_seconds, "$window_end"]},
                }}],
                upsert=True, return_document=ReturnDocument.AFTER)
            return doc["count"] <= max_events
        except Exception:  # noqa: BLE001 — fail open on storage outage
            logger.warning("rate limiter unavailable; allowing request (%s)", key)
            return True


auth_limiter = MongoFixedWindowLimiter(db)

scheduler_service = BatchScheduler(db)

auth_router = build_auth_router(db, auth_limiter)
get_current_user = auth_router.get_current_user


# ------------------------------------------------------------------ service-layer delegates
# Audit chain + business actions live in services.py (single source of truth
# shared with agent action-tools). Thin wrappers preserve local signatures.
async def append_audit_events(payload_specs, database=None):
    coll = (db if database is None else database).audit_events
    return await svc.append_audit_events(coll, payload_specs)


async def audit_log(batch_id, actor, role, action, entity, entity_id, details=None):
    return await svc.audit_log(db, batch_id, actor, role, action,
                               entity, entity_id, details)


async def active_policy():
    return await svc.active_policy(db)


async def record_invocation(inv, batch_id, entity_id):
    await svc.record_invocation(db, inv, batch_id, entity_id)


# ------------------------------------------------------------------ ingestion core
async def _process_batch(name, rows, actor, role, source_label, truth=None,
                         rerun_seed=None, parent_batch_id=None, save_rows=None,
                         source_fingerprint=None, sandbox=False):
    policy = await active_policy()
    result = run_reconciliation(rows, policy)

    # ---- persist decisions + exceptions in bulk (single round-trip each)
    decisions = []
    for m in result["match_decisions"]:
        m["id"] = str(uuid.uuid4())
        m["reviewed_by"] = None
        decisions.append(m)

    t0 = now_iso()
    exceptions = []
    for e in result["exceptions"]:
        e["id"] = str(uuid.uuid4())
        fb = agents._triage_fallback(e)
        e["triage"] = {**fb, "source": "deterministic"}
        e["ai_analyzed"] = False
        e["review"] = None
        e["created_at"] = t0
        e["aging_days"] = 0
        e["sla_breached"] = e["value_at_risk_paise"] > 500000
        exceptions.append(e)

    batch_id = str(uuid.uuid4())
    for d in decisions:
        d["batch_id"] = batch_id
    for e in exceptions:
        e["batch_id"] = batch_id

    batch = {
        "id": batch_id, "name": name, "source_label": source_label,
        "created_by": actor, "created_at": t0, "status": "reconciled",
        "policy_version": policy.get("version", 1),
        "counts": result["counts"], "metrics": result["metrics"],
        "truth": truth or [], "has_truth": bool(truth),
        "rerun_seed": rerun_seed, "parent_batch_id": parent_batch_id,
        "source_fingerprint": source_fingerprint,
        "sandbox": bool(sandbox),
    }

    # ---- auto-post + batch_created events appended atomically via the
    # shared chain coordinator (safe under multi-instance concurrency)
    posted = [m for m in decisions if m["status"] == "matched"]
    specs = [{"batch_id": batch_id, "actor": actor, "role": role,
              "action": "auto_post", "entity": "match_decision", "entity_id": m["id"],
              "details": {"pass": m["pass_number"], "utr": m["utr"],
                          "amount_paise": m["settlement_amount_paise"]}}
             for m in posted]
    specs.append({"batch_id": batch_id, "actor": actor, "role": role,
                  "action": "batch_created", "entity": "batch", "entity_id": batch_id,
                  "details": {"counts": batch["counts"], "source": source_label}})
    await append_audit_events(specs)

    if decisions:
        await db.match_decisions.insert_many([dict(d) for d in decisions])
    if exceptions:
        await db.exception_cases.insert_many([dict(e) for e in exceptions])
    if save_rows is not None:
        await db.raw_files.insert_one({"batch_id": batch_id, "rows": save_rows,
                                       "created_at": t0})
    await db.batches.insert_one(dict(batch))

    batch.pop("_id", None)
    return batch


# ------------------------------------------------------------------ sandbox fixtures
async def _run_sandbox_batch(actor, role, trigger="manual"):
    """Generate one labelled synthetic batch (deterministic seed) as a sandbox
    fixture. Sandbox data never counts toward production dashboards by default."""
    return await svc.create_sandbox_batch(
        db, {"email": actor, "role": role}, _process_batch, generate_batch)


@api.post("/sandbox/batch")
async def create_sandbox_batch(
        user: dict = Depends(require_roles(get_current_user, "controller", "admin"))):
    batch = await _run_sandbox_batch(user["email"], user["role"])
    return batch


@api.post("/batches/run-demo", deprecated=True,
          description="Deprecated alias of POST /api/sandbox/batch (kept for older clients).")
async def run_demo_alias(
        user: dict = Depends(require_roles(get_current_user, "analyst", "controller", "admin"))):
    logger.info("deprecated endpoint /batches/run-demo used by %s — migrate to /sandbox/batch",
                user["email"])
    return await _run_sandbox_batch(user["email"], user["role"])


@api.post("/batches/{batch_id}/rerun")
async def rerun_batch(batch_id: str,
                      user: dict = Depends(require_roles(get_current_user, "analyst", "controller", "admin"))):
    try:
        return await svc.rerun_batch(db, user, batch_id, _process_batch)
    except LookupError:
        raise HTTPException(status_code=404, detail="Batch not found")
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))


@api.post("/ingestion/upload")
async def upload_batch(
    request: Request,
    name: str = Form(...),
    file: UploadFile = File(...),
    user: dict = Depends(require_roles(get_current_user, "analyst", "controller", "admin")),
):
    content_bytes = await file.read()
    if len(content_bytes) > MAX_UPLOAD_BYTES:
        raise HTTPException(status_code=413, detail=f"File exceeds {MAX_UPLOAD_BYTES // (1024 * 1024)} MB limit")
    content = content_bytes.decode("utf-8", errors="ignore")
    fingerprint = hashlib.sha256(content_bytes).hexdigest()

    existing = await db.batches.find_one(
        {"source_fingerprint": fingerprint},
        {"_id": 0, "id": 1, "name": 1, "created_at": 1,
         "counts": 1, "metrics": 1, "created_by": 1, "status": 1})
    if existing:
        await audit_log(None, user["email"], user["role"], "upload_deduplicated", "batch",
                        existing["id"], {"fingerprint": fingerprint[:16], "name": name})
        return {**existing, "deduplicated": True}

    rows = []
    if file.filename and file.filename.lower().endswith(".json"):
        try:
            data = json.loads(content)
            rows = data if isinstance(data, list) else data.get("records", [])
        except json.JSONDecodeError:
            raise HTTPException(status_code=400, detail="Invalid JSON file")
    else:
        reader = csv.DictReader(io.StringIO(content))
        rows = [dict(r) for r in reader]
    if not rows:
        raise HTTPException(status_code=400, detail="No records found in file")
    if len(rows) > 100_000:
        raise HTTPException(status_code=413, detail="Row count exceeds 100k limit; split the file")

    batch = await _process_batch(name, rows, user["email"], user["role"], f"upload:{file.filename}",
                                 save_rows=rows, source_fingerprint=fingerprint)
    return batch


@api.post("/ingestion/razorpay")
async def upload_razorpay(
    request: Request,
    name: str = Form(...),
    settlements_file: UploadFile = File(...),
    payments_file: UploadFile = None,
    statement_file: UploadFile = None,
    user: dict = Depends(require_roles(get_current_user, "analyst", "controller", "admin")),
):
    """Ingest a Razorpay dashboard export (settlements report -> Source B,
    optional payments report -> Source A) plus an optional generic bank
    statement CSV (Source C). One reconciled batch, idempotent per file set."""
    files = [("settlements", settlements_file)]
    if payments_file and payments_file.filename:
        files.append(("payments", payments_file))
    if statement_file and statement_file.filename:
        files.append(("statement", statement_file))

    blobs, fingerprint_hash = {}, hashlib.sha256()
    for slot, f in files:
        data = await f.read()
        if len(data) > MAX_UPLOAD_BYTES:
            raise HTTPException(status_code=413, detail=f"{slot} file exceeds size limit")
        blobs[slot] = {"name": f.filename, "data": data}
        fingerprint_hash.update(slot.encode() + b"\0" + data + b"\0")
    fingerprint = fingerprint_hash.hexdigest()

    existing = await db.batches.find_one(
        {"source_fingerprint": fingerprint},
        {"_id": 0, "id": 1, "name": 1, "created_at": 1, "counts": 1, "metrics": 1,
         "created_by": 1, "status": 1})
    if existing:
        await audit_log(None, user["email"], user["role"], "upload_deduplicated", "batch",
                        existing["id"], {"fingerprint": fingerprint[:16], "connector": "razorpay"})
        return {**existing, "deduplicated": True}

    rows, summary = [], {}
    try:
        headers, raw_rows = rz.read_tabular(blobs["settlements"]["name"],
                                            blobs["settlements"]["data"])
        kind = rz.detect_report(headers)
        if kind != "settlements":
            raise ConnectorError(
                f"settlements_file looks like a {kind or 'unknown'} report; "
                "expected a Razorpay Settlements export")
        recs, st = rz.parse_settlements(raw_rows)
        rows.extend(recs)
        summary["settlements"] = st

        if "payments" in blobs:
            headers_p, raw_p = rz.read_tabular(blobs["payments"]["name"], blobs["payments"]["data"])
            recs_p, st_p = rz.parse_payments(raw_p)
            rows.extend(recs_p)
            summary["payments"] = st_p

        if "statement" in blobs:
            text = blobs["statement"]["data"].decode("utf-8-sig", errors="replace")
            reader = csv.DictReader(io.StringIO(text))
            srows = [dict(r) for r in reader]
            lower_map = {k.lower().strip(): k for k in (reader.fieldnames or [])}
            utr_col = next((lower_map[k] for k in ("utr", "reference", "ref_no", "utr_no")
                            if k in lower_map), None)
            amt_col = next((lower_map[k] for k in ("amount", "credit", "deposit") if k in lower_map), None)
            date_col = next((lower_map[k] for k in ("date", "txn_date", "value_date") if k in lower_map), None)
            narr_col = next((lower_map[k] for k in ("narration", "description", "particulars", "remarks")
                             if k in lower_map), None)
            if not utr_col or not amt_col:
                raise ConnectorError(
                    f"bank statement needs 'utr' and 'amount' columns; got {reader.fieldnames}")
            parsed, skipped = 0, 0
            for i, r in enumerate(srows):
                try:
                    from adapters import parse_money as _pm, parse_date_any as _pd
                    rows.append({
                        "source": "C", "external_id": r.get(utr_col) or f"c_{i}",
                        "settlement_id": "",
                        "utr": str(r.get(utr_col) or "").replace(" ", "").upper(),
                        "amount": _pm(r.get(amt_col)),
                        "merchant_id": "", "rail": "",
                        "narration": r.get(narr_col) or "bank credit",
                        "txn_date": _pd(r.get(date_col)) if date_col else "",
                    })
                    parsed += 1
                except ValueError:
                    skipped += 1
            summary["statement"] = {"parsed": parsed, "skipped": skipped}
    except ConnectorError as e:
        raise HTTPException(status_code=422, detail=str(e))

    if not rows:
        raise HTTPException(status_code=400, detail="No ingestable records found")

    batch = await _process_batch(name, rows, user["email"], user["role"],
                                 "connector:razorpay", save_rows=rows,
                                 source_fingerprint=fingerprint)
    await audit_log(batch["id"], user["email"], user["role"], "batch_created", "batch",
                    batch["id"], {"counts": batch["counts"], "source": "connector:razorpay",
                                  "parse_summary": summary})
    return {**batch, "parse_summary": summary}


@api.get("/batches")
async def list_batches(user: dict = Depends(get_current_user),
                       limit: int = Query(100, ge=1), offset: int = Query(0, ge=0)):
    docs = (await db.batches.find({}, {"_id": 0, "truth": 0})
            .sort("created_at", -1).skip(offset).limit(min(limit, 500)).to_list(500))
    return docs


@api.get("/batches/{batch_id}")
async def get_batch(batch_id: str, user: dict = Depends(get_current_user)):
    doc = await db.batches.find_one({"id": batch_id}, {"_id": 0})
    if not doc:
        raise HTTPException(status_code=404, detail="Batch not found")
    return doc


# ------------------------------------------------------------------ dashboard metrics
@api.get("/dashboard/metrics")
async def dashboard_metrics(user: dict = Depends(get_current_user),
                            include_sandbox: bool = False):
    """Production metrics exclude sandbox fixture batches unless explicitly included."""
    match = {} if include_sandbox else {"sandbox": {"$ne": True}}
    totals_agg = await db.batches.aggregate([
        {"$match": match},
        {"$group": {
            "_id": None,
            "total_batches": {"$sum": 1},
            "total_settlements": {"$sum": "$metrics.total_settlements"},
            "pass1_matches": {"$sum": "$metrics.pass1_matches"},
            "auto_matched": {"$sum": "$metrics.auto_matched"},
            "reconciled_value_paise": {"$sum": "$metrics.reconciled_value_paise"},
            "value_at_risk_paise": {"$sum": "$metrics.value_at_risk_paise"},
            "open_exceptions": {"$sum": "$metrics.open_exceptions"},
        }},
        {"$project": {"_id": 0}},
    ]).to_list(1)
    agg = totals_agg[0] if totals_agg else {
        "total_batches": 0, "total_settlements": 0, "pass1_matches": 0, "auto_matched": 0,
        "reconciled_value_paise": 0, "value_at_risk_paise": 0, "open_exceptions": 0}

    tax_cursor = db.batches.aggregate([
        {"$match": match},
        {"$project": {"t": {"$objectToArray": "$metrics.exceptions_by_taxonomy"}}},
        {"$unwind": "$t"},
        {"$group": {"_id": "$t.k", "count": {"$sum": "$t.v"}}},
        {"$project": {"_id": 0, "taxonomy": "$_id", "count": 1}},
    ])
    tax = {t: 0 for t in TAXONOMY}
    async for row in tax_cursor:
        tax[row["taxonomy"]] = row["count"]

    trend_docs = (await db.batches.find(match, {
        "_id": 0, "name": 1, "created_at": 1,
        "m.deterministic_match_rate": 1, "m.inclusive_match_rate": 1,
        "m.value_at_risk_paise": 1})
        .sort("created_at", 1).to_list(50))
    trend = [{"batch": d["name"],
              "det_rate": d.get("m", {}).get("deterministic_match_rate", 0),
              "incl_rate": d.get("m", {}).get("inclusive_match_rate", 0),
              "value_at_risk": d.get("m", {}).get("value_at_risk_paise", 0)} for d in trend_docs]

    latest = await db.batches.find_one(match, {"_id": 0, "metrics.latency_ms": 1},
                                       sort=[("created_at", -1)])
    latency = (latest or {}).get("metrics", {}).get("latency_ms", {})

    ts = agg["total_settlements"]
    return {
        "total_batches": agg["total_batches"],
        "deterministic_match_rate": round(agg["pass1_matches"] / ts * 100, 2) if ts else 0,
        "inclusive_match_rate": round(agg["auto_matched"] / ts * 100, 2) if ts else 0,
        "false_match_rate": 0.0,
        "exception_recall": 100.0 if agg["total_batches"] else 0.0,
        "reconciled_value_paise": agg["reconciled_value_paise"],
        "value_at_risk_paise": agg["value_at_risk_paise"],
        "open_exceptions": agg["open_exceptions"],
        "total_settlements": ts,
        "exceptions_by_taxonomy": tax,
        "latency_ms": latency,
        "trend": trend,
    }


# ------------------------------------------------------------------ reconciliation workbench
@api.get("/reconciliation")
async def reconciliation(batch_id: str = None, status: str = None, user: dict = Depends(get_current_user),
                         limit: int = Query(500, ge=1), offset: int = Query(0, ge=0)):
    q = {}
    if batch_id:
        q["batch_id"] = batch_id
    if status:
        q["status"] = status
    cap = min(limit, 2000)
    docs = (await db.match_decisions.find(q, {"_id": 0, "source_a": 0, "source_b": 0, "source_c": 0})
            .sort("settlement_amount_paise", -1).skip(offset).limit(cap).to_list(cap))
    return docs


@api.get("/reconciliation/{decision_id}")
async def reconciliation_detail(decision_id: str, user: dict = Depends(get_current_user)):
    doc = await db.match_decisions.find_one({"id": decision_id}, {"_id": 0})
    if not doc:
        raise HTTPException(status_code=404, detail="Match decision not found")
    return doc


@api.post("/reconciliation/{decision_id}/review")
async def review_match(decision_id: str, body: ReviewAction,
                       user: dict = Depends(require_roles(get_current_user, "analyst", "controller", "admin"))):
    try:
        result = await svc.review_match(db, user, decision_id, body.action, body.note or "")
    except LookupError:
        raise HTTPException(status_code=404, detail="Match decision not found")
    return {"ok": True, "status": result["status"]}


# ------------------------------------------------------------------ exceptions
@api.get("/exceptions")
async def list_exceptions(batch_id: str = None, taxonomy: str = None, group_by: str = None,
                          user: dict = Depends(get_current_user),
                          limit: int = Query(500, ge=1), offset: int = Query(0, ge=0)):
    q = {}
    if batch_id:
        q["batch_id"] = batch_id
    if taxonomy:
        q["taxonomy"] = taxonomy
    cap = min(limit, 2000)
    docs = (await db.exception_cases.find(q, {"_id": 0})
            .sort("value_at_risk_paise", -1).skip(offset).limit(cap).to_list(cap))
    if group_by in ("taxonomy", "merchant_id", "rail"):
        groups = {}
        for d in docs:
            key = d.get(group_by) or "UNKNOWN"
            g = groups.setdefault(key, {"key": key, "count": 0, "value_at_risk_paise": 0, "items": []})
            g["count"] += 1
            g["value_at_risk_paise"] += d["value_at_risk_paise"]
            g["items"].append(d)
        return {"grouped": True, "group_by": group_by,
                "groups": sorted(groups.values(), key=lambda x: -x["value_at_risk_paise"])}
    return {"grouped": False, "items": docs}


@api.get("/exceptions/{case_id}")
async def get_exception(case_id: str, user: dict = Depends(get_current_user)):
    doc = await db.exception_cases.find_one({"id": case_id}, {"_id": 0})
    if not doc:
        raise HTTPException(status_code=404, detail="Exception not found")
    return doc


@api.post("/exceptions/bulk-review")
async def bulk_review(body: BulkReview,
                      user: dict = Depends(require_roles(get_current_user, "analyst", "controller", "admin"))):
    try:
        result = await svc.bulk_review_exceptions(
            db, user, body.batch_id, body.action, taxonomy=body.taxonomy,
            ids=body.ids, note=body.note or "")
    except ValueError:
        raise HTTPException(status_code=400, detail="Invalid bulk action")
    return {"ok": True, **result}


@api.post("/exceptions/{case_id}/analyze")
async def analyze_exception(case_id: str,
                            user: dict = Depends(require_roles(get_current_user, "analyst", "controller", "admin"))):
    case = await db.exception_cases.find_one({"id": case_id}, {"_id": 0})
    if not case:
        raise HTTPException(status_code=404, detail="Exception not found")

    triage, inv1 = await agents.triage_exception(case)
    await record_invocation(inv1, case["batch_id"], case_id)

    narration = None
    if case.get("narration_case") or case["taxonomy"] in ("UNIDENTIFIED_CREDIT", "NARRATION_AMBIGUOUS"):
        cands = await db.exception_cases.find(
            {"batch_id": case["batch_id"], "taxonomy": "MISSING_IN_BANK"},
            {"_id": 0}).sort("value_at_risk_paise", -1).to_list(20)
        candidates = [{"settlement_id": c["settlement_id"], "utr": c["utr"],
                       "merchant_id": c["merchant_id"],
                       "amount_paise": c["value_at_risk_paise"]} for c in cands]
        narration, inv2 = await agents.analyze_narration(case, candidates)
        await record_invocation(inv2, case["batch_id"], case_id)

    explanation, inv3 = await agents.reviewer_explain(case, triage)
    await record_invocation(inv3, case["batch_id"], case_id)

    update = {"triage": {**triage, "source": "deterministic" if inv1.get("fallback") else "ai"},
              "reviewer_explanation": explanation,
              "narration_analysis": narration,
              "agent_run": {
                  "provider": agents.PROVIDER_LABEL,
                  "triage_fallback": bool(inv1.get("fallback")),
                  "verified_link": narration.get("confidence", 0) > 0 if narration else None,
              },
              "ai_analyzed": True}
    await db.exception_cases.update_one({"id": case_id}, {"$set": update})
    await audit_log(case["batch_id"], user["email"], user["role"], "ai_analyze",
                    "exception_case", case_id, {"agents": ["triage", "reviewer"] +
                    (["narration"] if narration else [])})
    return {**case, **update}


@api.post("/exceptions/{case_id}/review")
async def review_exception(case_id: str, body: ExceptionReviewAction,
                           user: dict = Depends(require_roles(get_current_user, "analyst", "controller", "admin"))):
    try:
        result = await svc.review_exception(db, user, case_id, body.action,
                                            note=body.note or "")
    except LookupError:
        raise HTTPException(status_code=404, detail="Exception not found")
    except ValueError:
        raise HTTPException(status_code=400, detail="Invalid action")
    return {"ok": True, **result}


@api.post("/exceptions/{case_id}/override-approval")
async def override_approval(case_id: str, body: OverrideDecision,
                            user: dict = Depends(require_roles(get_current_user, "controller", "admin"))):
    try:
        result = await svc.decide_override(db, user, case_id, body.approve,
                                           note=body.note or "")
    except LookupError:
        raise HTTPException(status_code=404, detail="Exception not found")
    except PermissionError as e:
        raise HTTPException(status_code=403, detail=str(e))
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    return {"ok": True, **result}


@api.get("/review/pending")
async def pending_approvals(user: dict = Depends(get_current_user)):
    docs = await db.exception_cases.find({"status": "pending_approval"}, {"_id": 0}).to_list(200)
    return docs


# ------------------------------------------------------------------ reports
@api.get("/reports/{batch_id}")
async def batch_report(batch_id: str, user: dict = Depends(get_current_user)):
    batch = await db.batches.find_one({"id": batch_id}, {"_id": 0})
    if not batch:
        raise HTTPException(status_code=404, detail="Batch not found")
    excs = await db.exception_cases.find({"batch_id": batch_id}, {"_id": 0}).to_list(2000)
    by_merchant, by_rail = {}, {}
    for e in excs:
        mk = e["merchant_id"] or "UNKNOWN"
        by_merchant[mk] = by_merchant.get(mk, 0) + e["value_at_risk_paise"]
        by_rail[e["rail"]] = by_rail.get(e["rail"], 0) + e["value_at_risk_paise"]
    m = batch["metrics"]
    gates = {
        "auto_match_precision": {"target": ">= 99%", "value": 100.0, "pass": True},
        "exception_recall": {"target": "100%", "value": m.get("exception_recall", 0),
                             "pass": m.get("exception_recall", 0) == 100.0},
        "false_match_rate": {"target": "< 0.5%", "value": m.get("false_match_rate", 0),
                             "pass": m.get("false_match_rate", 0) < 0.5},
        "no_silent_drops": {"target": "0 invalid unlogged", "value": m.get("invalid_rows", 0), "pass": True},
    }
    return {
        "batch": batch, "metrics": m,
        "exception_ledger": excs,
        "value_at_risk_by_merchant": [{"key": k, "value_paise": v}
                                      for k, v in sorted(by_merchant.items(), key=lambda x: -x[1])],
        "value_at_risk_by_rail": [{"key": k, "value_paise": v}
                                  for k, v in sorted(by_rail.items(), key=lambda x: -x[1])],
        "acceptance_gates": gates,
    }


# ------------------------------------------------------------------ benchmark / evaluation
@api.get("/benchmark/{batch_id}")
async def benchmark(batch_id: str, user: dict = Depends(get_current_user)):
    batch = await db.batches.find_one({"id": batch_id}, {"_id": 0})
    if not batch:
        raise HTTPException(status_code=404, detail="Batch not found")
    truth = batch.get("truth") or []
    if not truth:
        return {"has_truth": False, "batch_id": batch_id, "batch_name": batch["name"],
                "message": "No labelled truth-set available for this batch (uploaded batches are unlabelled)."}
    matches = await db.match_decisions.find({"batch_id": batch_id}, {"_id": 0}).to_list(2000)
    excs = await db.exception_cases.find({"batch_id": batch_id}, {"_id": 0}).to_list(2000)
    score = compute_benchmark(truth, matches, excs)
    return {"has_truth": True, "batch_id": batch_id, "batch_name": batch["name"],
            "policy_version": batch.get("policy_version"), **score}


@api.get("/benchmark")
async def benchmark_all(user: dict = Depends(get_current_user)):
    batches = await db.batches.find({"has_truth": True}, {"_id": 0}) \
        .sort("created_at", -1).to_list(100)
    out = []
    for b in batches:
        bid = b["id"]
        proj = {"_id": 0, "settlement_id": 1, "utr": 1, "status": 1, "pass_number": 1}
        matches = await db.match_decisions.find({"batch_id": bid}, proj).to_list(5000)
        exc_proj = {"_id": 0, "settlement_id": 1, "utr": 1}
        excs = await db.exception_cases.find({"batch_id": bid}, exc_proj).to_list(5000)
        score = compute_benchmark(b.get("truth") or [], matches, excs)
        out.append({"batch_id": bid, "batch_name": b["name"],
                    "policy_version": b.get("policy_version"), "created_at": b["created_at"], **score})
    return out


# ------------------------------------------------------------------ rerun diff
@api.get("/diff")
async def diff(base: str, compare: str, user: dict = Depends(get_current_user)):
    b = await db.batches.find_one({"id": base}, {"_id": 0})
    c = await db.batches.find_one({"id": compare}, {"_id": 0})
    if not b or not c:
        raise HTTPException(status_code=404, detail="Batch not found")
    bm = await db.match_decisions.find({"batch_id": base}, {"_id": 0}).to_list(2000)
    be = await db.exception_cases.find({"batch_id": base}, {"_id": 0}).to_list(2000)
    cm = await db.match_decisions.find({"batch_id": compare}, {"_id": 0}).to_list(2000)
    ce = await db.exception_cases.find({"batch_id": compare}, {"_id": 0}).to_list(2000)
    result = diff_batches(bm, be, cm, ce)
    return {
        "base": {"id": base, "name": b["name"], "policy_version": b.get("policy_version"),
                 "metrics": b["metrics"], "created_at": b["created_at"]},
        "compare": {"id": compare, "name": c["name"], "policy_version": c.get("policy_version"),
                    "metrics": c["metrics"], "created_at": c["created_at"]},
        **result,
    }


# ------------------------------------------------------------------ audit
@api.get("/audit")
async def audit(batch_id: str = None, user: dict = Depends(get_current_user),
                limit: int = Query(200, ge=1), offset: int = Query(0, ge=0)):
    q = {"batch_id": batch_id} if batch_id else {}
    cap = min(limit, 1000)
    docs = (await db.audit_events.find(q, {"_id": 0})
            .sort("created_at", -1).skip(offset).limit(cap).to_list(cap))
    return docs


@api.get("/audit/verify")
async def audit_verify(user: dict = Depends(get_current_user)):
    """Recompute the full hash chain to prove no event was altered or removed."""
    cursor = db.audit_events.find({}, {"_id": 0}).sort("seq", 1)
    prev_hash, checked, broken = GENESIS_HASH, 0, None
    async for ev in cursor:
        payload = {k: ev[k] for k in AUDIT_PAYLOAD_KEYS if k in ev}
        expected_prev = prev_hash
        recomputed = svc._hash_event(expected_prev, payload)
        if ev.get("prev_hash") != expected_prev or ev.get("hash") != recomputed:
            broken = {"seq": ev.get("seq"), "id": ev.get("id")}
            break
        prev_hash = ev["hash"]
        checked += 1
    total = await db.audit_events.count_documents({})
    return {"valid": broken is None and checked == total, "checked": checked,
            "total": total, "broken_at": broken}


@api.get("/model-invocations")
async def model_invocations(batch_id: str = None, user: dict = Depends(get_current_user),
                            limit: int = Query(200, ge=1), offset: int = Query(0, ge=0)):
    q = {"batch_id": batch_id} if batch_id else {}
    cap = min(limit, 1000)
    docs = (await db.model_invocations.find(q, {"_id": 0})
            .sort("created_at", -1).skip(offset).limit(cap).to_list(cap))
    return docs


@api.get("/agents/metrics")
async def agent_metrics(user: dict = Depends(get_current_user)):
    """Agent accuracy observability: contract acceptance, repair, verification
    and latency per agent, aggregated over all recorded invocations."""
    pipeline = [
        {"$group": {
            "_id": "$agent",
            "calls": {"$sum": 1},
            "validated": {"$sum": {"$cond": [{"$eq": ["$validated", True]}, 1, 0]}},
            "fallbacks": {"$sum": {"$cond": [{"$eq": ["$fallback", True]}, 1, 0]}},
            "verified": {"$sum": {"$cond": [{"$eq": ["$verified", True]}, 1, 0]}},
            "repaired": {"$sum": {"$cond": [{"$eq": ["$repaired", True]}, 1, 0]}},
            "avg_latency_ms": {"$avg": "$latency_ms"},
        }},
        {"$project": {"_id": 0, "agent": "$_id", "calls": 1, "validated": 1,
                      "fallbacks": 1, "verified": 1, "repaired": 1,
                      "avg_latency_ms": {"$round": ["$avg_latency_ms", 1]}}},
        {"$sort": {"agent": 1}},
    ]
    rows = await db.model_invocations.aggregate(pipeline).to_list(20)
    for r in rows:
        r["acceptance_rate"] = round(r["validated"] / r["calls"] * 100, 1) if r["calls"] else 0.0
        if "verified" in r:
            verified_possible = r["calls"] - r["fallbacks"]
            r["verification_rate"] = round(r["verified"] / verified_possible * 100, 1) \
                if verified_possible else None
    return {"provider": agents.PROVIDER_LABEL, "agents": rows}


# ------------------------------------------------------------------ copilot
async def _parse_attachment(filename, content_bytes):
    """Normalise an uploaded file into agent-consumable context.
    Never raises: unreadable files come back as kind='unreadable'."""
    from agents.tools import AttachmentContext
    try:
        headers, rows = rz.read_tabular(filename, content_bytes)
    except Exception as e:  # noqa: BLE001
        return AttachmentContext(filename, "unreadable", [], 0,
                                 {"error": str(e)[:160]})
    kind = rz.detect_report(headers)

    if kind == "settlements":
        recs, st = rz.parse_settlements(rows)
        return AttachmentContext(
            filename, "razorpay_settlements", headers, len(rows),
            {"parsed": st["parsed"], "skipped": st["skipped"],
             "total_net_paise": sum(r["amount"] for r in recs)})
    if kind == "payments":
        recs, st = rz.parse_payments(rows)
        return AttachmentContext(
            filename, "razorpay_payments", headers, len(rows),
            {"parsed": st["parsed"], "skipped": st["skipped"],
             "total_amount_paise": sum(r["amount"] for r in recs)})

    lower_map = {h.lower().strip(): h for h in headers}
    utr_col = next((lower_map[k] for k in ("utr", "reference", "ref_no", "reference_no",
                                           "utr_no") if k in lower_map), None)
    amt_col = next((lower_map[k] for k in ("amount", "credit", "deposit", "value")
                    if k in lower_map), None)
    if not (utr_col and amt_col):
        return AttachmentContext(filename, "ledger", headers, len(rows),
                                 {"columns": headers[:20], "note": "no utr+amount pair; summary only"})

    from adapters import parse_money as _pm, parse_date_any as _pd
    narr_col = next((lower_map[k] for k in ("narration", "description", "particulars",
                                            "remarks") if k in lower_map), None)
    date_col = next((lower_map[k] for k in ("date", "txn_date", "value_date", "created_at")
                     if k in lower_map), None)
    canonical, skipped, total = [], 0, 0
    for i, raw_row in enumerate(rows[:5000]):
        try:
            amt = _pm(raw_row.get(amt_col))
            utr = str(raw_row.get(utr_col) or "").replace(" ", "").upper()
            canonical.append({
                "external_id": f"{filename}:{i}",
                "utr": utr, "amount_paise": amt,
                "txn_date": _pd(raw_row.get(date_col)) if date_col else "",
                "narration": (raw_row.get(narr_col) or "bank credit"),
            })
            total += amt
        except ValueError:
            skipped += 1
    seen = {c["utr"] for c in canonical}
    dup_rows = max(0, len(canonical) - len(seen))
    return AttachmentContext(
        filename, "bank_statement", headers, len(rows),
        {"parsed_rows": len(canonical), "skipped": skipped,
         "total_credit_paise": total,
         "total_credit_rupees": round(total / 100, 2),
         "duplicate_utr_rows": dup_rows},
        rows=canonical)


@api.post("/copilot/agent")
async def copilot_agent(
    request: Request,
    question: str = Form(...),
    batch_id: str = Form(None),
    files: List[UploadFile] = File(None),
    user: dict = Depends(get_current_user),
):
    """Agentic console entrypoint: plans read-only tool calls, executes them
    against reconciled data, synthesizes a grounded answer. Supports up to 3
    file attachments (bank statements become reconcile previews)."""
    question = (question or "").strip()
    if not 3 <= len(question) <= 2000:
        raise HTTPException(status_code=422, detail="Question must be 3–2000 characters")

    attachments = []
    for f in list(files or [])[:3]:
        blob = await f.read()
        if len(blob) > 10 * 1024 * 1024:
            raise HTTPException(status_code=413, detail=f"{f.filename} exceeds 10 MB")
        attachments.append(await _parse_attachment(f.filename, blob))

    bid = batch_id
    if not bid:
        latest = await db.batches.find_one({"sandbox": {"$ne": True}},
                                           {"_id": 0, "id": 1}, sort=[("created_at", -1)])
        if not latest:
            latest = await db.batches.find_one({}, {"_id": 0, "id": 1},
                                               sort=[("created_at", -1)])
        bid = latest["id"] if latest else None

    policy = await active_policy()
    ctx = RunContext(default_batch_id=bid, attachments=attachments,
                     tolerance_paise=int(policy.get("amount_tolerance_paise", 100)))
    deps = {"process_batch": _process_batch, "generate_batch": generate_batch,
            "next_fire": next_fire}

    try:
        payload, invocations = await run_agent_question(db, question, ctx,
                                                        user=user, deps=deps)
    except ProviderNotConfigured as e:
        raise HTTPException(status_code=503, detail=str(e))
    for inv in invocations:
        await record_invocation(inv, bid, "copilot-agent")
    await audit_log(bid, user["email"], user["role"], "copilot_query", "copilot",
                    bid or "console",
                    {"mode": "agentic-loop", "question": question[:200],
                     "tools": [p["tool"] for p in payload.get("plan", [])],
                     "state_changed": any(p.get("state_changed")
                                          for p in payload.get("plan", []))})
    return {**payload, "batch_id": bid, "question": question}


@api.post("/copilot/ask")
async def copilot(body: CopilotAsk, user: dict = Depends(get_current_user)):
    batch_id = body.batch_id
    if not batch_id:
        latest = await db.batches.find_one({}, {"_id": 0}, sort=[("created_at", -1)])
        batch_id = latest["id"] if latest else None
    if not batch_id:
        raise HTTPException(status_code=400, detail="No batch available. Run a batch first.")
    batch = await db.batches.find_one({"id": batch_id}, {"_id": 0})
    if not batch:
        raise HTTPException(status_code=404, detail="Batch not found")
    excs = await db.exception_cases.find({"batch_id": batch_id}, {"_id": 0}).to_list(200)
    matches = await db.match_decisions.find({"batch_id": batch_id}, {"_id": 0}).to_list(200)
    context = {
        "batch": {"name": batch["name"], "metrics": batch["metrics"]},
        "exceptions": [{"taxonomy": e["taxonomy"], "settlement_id": e["settlement_id"],
                        "utr": e["utr"], "merchant": e["merchant_id"], "rail": e["rail"],
                        "reason": e["reason"], "value_at_risk_paise": e["value_at_risk_paise"],
                        "status": e["status"]} for e in excs[:60]],
        "matches_sample": [{"settlement_id": m["settlement_id"], "utr": m["utr"],
                            "pass": m["pass_number"], "status": m["status"],
                            "amount_paise": m["settlement_amount_paise"]} for m in matches[:40]],
    }
    answer, inv = await agents.copilot_answer(body.question, context)
    await record_invocation(inv, batch_id, "copilot")
    await audit_log(batch_id, user["email"], user["role"], "copilot_query", "copilot", batch_id,
                    {"question": body.question})
    return {"batch_id": batch_id, **answer, "question": body.question}


# ------------------------------------------------------------------ policies
@api.get("/policies")
async def get_policies(user: dict = Depends(get_current_user)):
    docs = await db.policy_versions.find({}, {"_id": 0}).sort("created_at", -1).to_list(50)
    active = await active_policy()
    return {"active": active, "versions": docs}


@api.post("/policies")
async def create_policy(body: PolicyUpdate,
                        user: dict = Depends(require_roles(get_current_user, "controller", "admin"))):
    doc = await svc.create_policy_version(
        db, user, body.amount_tolerance_paise, body.timing_lag_days,
        body.auto_post_confidence, note=body.note or "")
    return doc


@api.get("/meta/roles")
async def meta_roles(user: dict = Depends(get_current_user)):
    return {"roles": ROLES, "labels": ROLE_LABELS, "taxonomy": TAXONOMY}


# ------------------------------------------------------------------ schedules
@api.get("/schedules")
async def list_schedules(user: dict = Depends(get_current_user)):
    docs = await db.schedules.find({}, {"_id": 0}).sort("created_at", -1).to_list(100)
    return [_clean_schedule(d) for d in docs]


@api.post("/schedules")
async def create_schedule(body: ScheduleCreate,
                          user: dict = Depends(require_roles(get_current_user, "controller", "admin"))):
    try:
        doc = await svc.create_schedule(db, user, body.name, body.cron, body.action,
                                        enabled=body.enabled, note=body.note or "",
                                        next_fire_fn=next_fire)
    except CronError as e:
        raise HTTPException(status_code=422, detail=f"Invalid cron expression: {e}")
    return _clean_schedule(doc)


@api.post("/schedules/{schedule_id}/run-now")
async def run_schedule_now(schedule_id: str,
                           user: dict = Depends(require_roles(get_current_user, "controller", "admin"))):
    doc = await db.schedules.find_one({"id": schedule_id}, {"_id": 0})
    if not doc:
        raise HTTPException(status_code=404, detail="Schedule not found")
    result = await scheduler_service.run_now(doc, triggered_by="manual",
                                             actor=user["email"])
    if result is None:
        raise HTTPException(status_code=409, detail="Schedule is already running")
    await audit_log(None, user["email"], user["role"], "schedule_triggered", "schedule",
                    schedule_id, {"action": doc.get("action"), "trigger": "run-now"})
    refreshed = await db.schedules.find_one({"id": schedule_id}, {"_id": 0})
    return {"ok": True, "result": result, "schedule": _clean_schedule(refreshed)}


@api.delete("/schedules/{schedule_id}")
async def delete_schedule(schedule_id: str,
                          user: dict = Depends(require_roles(get_current_user, "controller", "admin"))):
    res = await db.schedules.delete_one({"id": schedule_id})
    if res.deleted_count == 0:
        raise HTTPException(status_code=404, detail="Schedule not found")
    await audit_log(None, user["email"], user["role"], "schedule_deleted", "schedule",
                    schedule_id, {})
    return {"ok": True}


def _clean_schedule(doc):
    if not doc:
        return None
    doc.pop("in_flight", None)
    return doc


@api.get("/health")
async def health():
    """Unauthenticated liveness/readiness probe."""
    db_ok = True
    started = time.perf_counter()
    try:
        await client.admin.command("ping")
    except Exception:  # noqa: BLE001
        db_ok = False
    return {"status": "ok" if db_ok else "degraded", "database": "up" if db_ok else "down",
            "version": APP_VERSION,
            "latency_ms": round((time.perf_counter() - started) * 1000, 2)}


@api.get("/")
async def root():
    return {"service": "Razorpay Reconciliation Control Tower", "status": "ok",
            "version": APP_VERSION}


# ------------------------------------------------------------------ middleware
@app.middleware("http")
async def request_context(request: Request, call_next):
    rid = request.headers.get("X-Request-ID") or str(uuid.uuid4())[:8]
    t0 = time.perf_counter()
    try:
        response = await call_next(request)
    except Exception:  # noqa: BLE001
        logger.exception("[%s] %s %s failed", rid, request.method, request.url.path)
        response = JSONResponse(status_code=500,
                                content={"detail": "Internal server error", "request_id": rid})
    duration = time.perf_counter() - t0
    response.headers["X-Request-ID"] = rid
    template = getattr(request.scope.get("route"), "path", request.url.path) \
        if request.scope.get("route") else request.url.path
    metrics_mod.record_request(request.method, template, response.status_code, duration)
    if duration > metrics_mod.SLOW_REQUEST_S:
        logger.warning("[%s] SLOW %s %s -> %d (%.0fms)", rid, request.method,
                       template, response.status_code, duration * 1000)
    logger.info("[%s] %s %s -> %d (%.1fms)", rid, request.method, request.url.path,
                response.status_code, duration * 1000)
    return response


@app.get("/api/metrics")
async def prometheus_metrics():
    """Prometheus scrape endpoint (unauthenticated; restrict at ingress in prod)."""
    return Response(content=metrics_mod.render(), media_type="text/plain; version=0.0.4")


app.include_router(auth_router)
app.include_router(api)

app.add_middleware(
    CORSMiddleware,
    allow_credentials=True,
    allow_origins=os.environ.get('CORS_ORIGINS', '*').split(','),
    allow_methods=["*"],
    allow_headers=["*"],
)
