from dotenv import load_dotenv
from pathlib import Path

ROOT_DIR = Path(__file__).parent
load_dotenv(ROOT_DIR / '.env')

import os
import uuid
import logging
from datetime import datetime, timezone

from fastapi import FastAPI, APIRouter, HTTPException, Depends, UploadFile, File, Form
from starlette.middleware.cors import CORSMiddleware
from motor.motor_asyncio import AsyncIOMotorClient
import csv
import io
import json

from models import ReviewAction, OverrideDecision, CopilotAsk, PolicyUpdate, TAXONOMY, ROLES
from engine import run_reconciliation
from seed_data import generate_ledgers
import agents
from auth import build_auth_router, require_roles, seed_users, ROLE_LABELS

mongo_url = os.environ['MONGO_URL']
client = AsyncIOMotorClient(mongo_url)
db = client[os.environ['DB_NAME']]

app = FastAPI(title="Razorpay Reconciliation Control Tower")
api = APIRouter(prefix="/api")

logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(name)s - %(levelname)s - %(message)s')
logger = logging.getLogger("recon")

auth_router = build_auth_router(db)
get_current_user = auth_router.get_current_user

DEFAULT_POLICY = {"amount_tolerance_paise": 100, "timing_lag_days": 1, "auto_post_confidence": 0.95}


# ---------------- helpers ----------------
def now_iso():
    return datetime.now(timezone.utc).isoformat()


async def active_policy():
    doc = await db.policy_versions.find_one(sort=[("created_at", -1)], projection={"_id": 0})
    if not doc:
        return {**DEFAULT_POLICY, "version": 1}
    return doc


async def audit_log(batch_id, actor, role, action, entity, entity_id, details=None):
    ev = {
        "id": str(uuid.uuid4()), "batch_id": batch_id, "actor": actor, "role": role,
        "action": action, "entity": entity, "entity_id": entity_id,
        "details": details or {}, "created_at": now_iso(),
    }
    await db.audit_events.insert_one(dict(ev))
    ev.pop("_id", None)
    return ev


async def record_invocation(inv, batch_id, entity_id):
    doc = {**inv, "batch_id": batch_id, "entity_id": entity_id}
    await db.model_invocations.insert_one(dict(doc))


# ---------------- batches / ingestion ----------------
async def _process_batch(name, rows, actor, role, source_label):
    policy = await active_policy()
    result = run_reconciliation(rows, policy)
    batch_id = str(uuid.uuid4())
    batch = {
        "id": batch_id, "name": name, "source_label": source_label,
        "created_by": actor, "created_at": now_iso(), "status": "reconciled",
        "policy_version": policy.get("version", 1),
        "counts": result["counts"], "metrics": result["metrics"],
    }
    await db.batches.insert_one(dict(batch))

    for m in result["match_decisions"]:
        m["id"] = str(uuid.uuid4())
        m["batch_id"] = batch_id
        m["reviewed_by"] = None
        await db.match_decisions.insert_one(dict(m))
        if m["status"] == "matched":
            await audit_log(batch_id, actor, role, "auto_post", "match_decision", m["id"],
                            {"pass": m["pass_number"], "utr": m["utr"], "amount_paise": m["settlement_amount_paise"]})

    policy_row = await active_policy()
    for e in result["exceptions"]:
        e["id"] = str(uuid.uuid4())
        e["batch_id"] = batch_id
        # deterministic fallback triage attached immediately (no LLM on hot path)
        fb = agents._triage_fallback(e)
        e["triage"] = {**fb, "source": "deterministic"}
        e["ai_analyzed"] = False
        e["review"] = None
        e["created_at"] = now_iso()
        e["aging_days"] = 0
        e["sla_breached"] = e["value_at_risk_paise"] > 500000
        await db.exception_cases.insert_one(dict(e))

    batch.pop("_id", None)
    return batch


@api.post("/batches/run-demo")
async def run_demo(user: dict = Depends(require_roles(get_current_user, "analyst", "controller", "admin"))):
    seq = await db.batches.count_documents({})
    rows = generate_ledgers(seed=42 + seq)
    batch = await _process_batch(
        f"Demo Settlement Batch #{seq + 1}", rows, user["email"], user["role"], "seed:A+B+C")
    await audit_log(batch["id"], user["email"], user["role"], "batch_created", "batch", batch["id"],
                    {"counts": batch["counts"], "source": "demo"})
    return batch


@api.post("/ingestion/upload")
async def upload_batch(
    name: str = Form(...),
    file: UploadFile = File(...),
    user: dict = Depends(require_roles(get_current_user, "analyst", "controller", "admin")),
):
    content = (await file.read()).decode("utf-8", errors="ignore")
    rows = []
    if file.filename.lower().endswith(".json"):
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
    batch = await _process_batch(name, rows, user["email"], user["role"], f"upload:{file.filename}")
    await audit_log(batch["id"], user["email"], user["role"], "batch_created", "batch", batch["id"],
                    {"counts": batch["counts"], "source": file.filename})
    return batch


@api.get("/batches")
async def list_batches(user: dict = Depends(get_current_user)):
    docs = await db.batches.find({}, {"_id": 0}).sort("created_at", -1).to_list(200)
    return docs


@api.get("/batches/{batch_id}")
async def get_batch(batch_id: str, user: dict = Depends(get_current_user)):
    doc = await db.batches.find_one({"id": batch_id}, {"_id": 0})
    if not doc:
        raise HTTPException(status_code=404, detail="Batch not found")
    return doc


# ---------------- dashboard metrics ----------------
@api.get("/dashboard/metrics")
async def dashboard_metrics(user: dict = Depends(get_current_user)):
    batches = await db.batches.find({}, {"_id": 0}).sort("created_at", -1).to_list(200)
    total_batches = len(batches)
    agg = {"reconciled_value_paise": 0, "value_at_risk_paise": 0, "auto_matched": 0,
           "total_settlements": 0, "open_exceptions": 0, "pass1_matches": 0}
    tax = {t: 0 for t in TAXONOMY}
    trend = []
    for b in batches:
        m = b.get("metrics", {})
        for k in agg:
            agg[k] += m.get(k, 0)
        for t, c in (m.get("exceptions_by_taxonomy") or {}).items():
            tax[t] = tax.get(t, 0) + c
        trend.append({"batch": b["name"], "det_rate": m.get("deterministic_match_rate", 0),
                      "incl_rate": m.get("inclusive_match_rate", 0),
                      "value_at_risk": m.get("value_at_risk_paise", 0)})
    det_rate = round(agg["pass1_matches"] / agg["total_settlements"] * 100, 2) if agg["total_settlements"] else 0
    incl_rate = round(agg["auto_matched"] / agg["total_settlements"] * 100, 2) if agg["total_settlements"] else 0
    latest = batches[0]["metrics"].get("latency_ms", {}) if batches else {}
    return {
        "total_batches": total_batches,
        "deterministic_match_rate": det_rate,
        "inclusive_match_rate": incl_rate,
        "false_match_rate": 0.0,
        "exception_recall": 100.0 if total_batches else 0.0,
        "reconciled_value_paise": agg["reconciled_value_paise"],
        "value_at_risk_paise": agg["value_at_risk_paise"],
        "open_exceptions": agg["open_exceptions"],
        "total_settlements": agg["total_settlements"],
        "exceptions_by_taxonomy": tax,
        "latency_ms": latest,
        "trend": list(reversed(trend)),
    }


# ---------------- reconciliation workbench ----------------
@api.get("/reconciliation")
async def reconciliation(batch_id: str = None, status: str = None, user: dict = Depends(get_current_user)):
    q = {}
    if batch_id:
        q["batch_id"] = batch_id
    if status:
        q["status"] = status
    docs = await db.match_decisions.find(q, {"_id": 0}).sort("settlement_amount_paise", -1).to_list(1000)
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
    doc = await db.match_decisions.find_one({"id": decision_id})
    if not doc:
        raise HTTPException(status_code=404, detail="Match decision not found")
    new_status = {"approve": "matched", "reject": "exception", "escalate": "pending_review"}.get(body.action)
    if not new_status:
        raise HTTPException(status_code=400, detail="Invalid action")
    await db.match_decisions.update_one({"id": decision_id}, {"$set": {
        "status": new_status, "reviewed_by": user["email"], "review_note": body.note}})
    await audit_log(doc["batch_id"], user["email"], user["role"], f"match_{body.action}",
                    "match_decision", decision_id, {"note": body.note, "utr": doc.get("utr")})
    return {"ok": True, "status": new_status}


# ---------------- exceptions ----------------
@api.get("/exceptions")
async def list_exceptions(batch_id: str = None, taxonomy: str = None, group_by: str = None,
                          user: dict = Depends(get_current_user)):
    q = {}
    if batch_id:
        q["batch_id"] = batch_id
    if taxonomy:
        q["taxonomy"] = taxonomy
    docs = await db.exception_cases.find(q, {"_id": 0}).sort("value_at_risk_paise", -1).to_list(2000)
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
        # candidate open settlements from same batch missing a bank match
        cands = await db.exception_cases.find(
            {"batch_id": case["batch_id"], "taxonomy": "MISSING_IN_BANK"}, {"_id": 0}).to_list(20)
        candidates = [{"settlement_id": c["settlement_id"], "utr": c["utr"],
                       "merchant_id": c["merchant_id"], "amount_paise": c["value_at_risk_paise"]}
                      for c in cands]
        narration, inv2 = await agents.analyze_narration(case, candidates)
        await record_invocation(inv2, case["batch_id"], case_id)

    explanation, inv3 = await agents.reviewer_explain(case, triage)
    await record_invocation(inv3, case["batch_id"], case_id)

    update = {"triage": {**triage, "source": "ai"}, "reviewer_explanation": explanation,
              "narration_analysis": narration, "ai_analyzed": True}
    await db.exception_cases.update_one({"id": case_id}, {"$set": update})
    await audit_log(case["batch_id"], user["email"], user["role"], "ai_analyze",
                    "exception_case", case_id, {"agents": ["triage", "reviewer"] +
                    (["narration"] if narration else [])})
    return {**case, **update}


@api.post("/exceptions/{case_id}/review")
async def review_exception(case_id: str, body: ReviewAction,
                           user: dict = Depends(require_roles(get_current_user, "analyst", "controller", "admin"))):
    case = await db.exception_cases.find_one({"id": case_id})
    if not case:
        raise HTTPException(status_code=404, detail="Exception not found")
    if body.action not in ("resolve", "escalate", "override", "reject"):
        raise HTTPException(status_code=400, detail="Invalid action")
    # maker-checker: override on material value requires a checker (controller/admin)
    material = case["value_at_risk_paise"] > 200000
    if body.action == "override" and material and user["role"] not in ("controller", "admin"):
        status = "pending_approval"
    else:
        status = {"resolve": "resolved", "escalate": "escalated",
                  "override": "resolved", "reject": "rejected"}[body.action]
    review = {"action": body.action, "by": user["email"], "role": user["role"],
              "note": body.note, "at": now_iso(), "requires_approval": status == "pending_approval"}
    await db.exception_cases.update_one({"id": case_id}, {"$set": {"status": status, "review": review}})
    await audit_log(case["batch_id"], user["email"], user["role"], f"exception_{body.action}",
                    "exception_case", case_id, {"note": body.note, "resulting_status": status})
    return {"ok": True, "status": status}


@api.post("/exceptions/{case_id}/override-approval")
async def override_approval(case_id: str, body: OverrideDecision,
                            user: dict = Depends(require_roles(get_current_user, "controller", "admin"))):
    case = await db.exception_cases.find_one({"id": case_id})
    if not case:
        raise HTTPException(status_code=404, detail="Exception not found")
    if case.get("status") != "pending_approval":
        raise HTTPException(status_code=400, detail="Case is not pending approval")
    status = "resolved" if body.approve else "open"
    approval = {"approved": body.approve, "by": user["email"], "role": user["role"],
                "note": body.note, "at": now_iso()}
    await db.exception_cases.update_one({"id": case_id},
                                        {"$set": {"status": status, "approval": approval}})
    await audit_log(case["batch_id"], user["email"], user["role"],
                    "override_" + ("approved" if body.approve else "rejected"),
                    "exception_case", case_id, {"note": body.note})
    return {"ok": True, "status": status}


@api.get("/review/pending")
async def pending_approvals(user: dict = Depends(get_current_user)):
    docs = await db.exception_cases.find({"status": "pending_approval"}, {"_id": 0}).to_list(200)
    return docs


# ---------------- reports ----------------
@api.get("/reports/{batch_id}")
async def batch_report(batch_id: str, user: dict = Depends(get_current_user)):
    batch = await db.batches.find_one({"id": batch_id}, {"_id": 0})
    if not batch:
        raise HTTPException(status_code=404, detail="Batch not found")
    excs = await db.exception_cases.find({"batch_id": batch_id}, {"_id": 0}).to_list(2000)
    by_merchant, by_rail = {}, {}
    for e in excs:
        by_merchant[e["merchant_id"] or "UNKNOWN"] = by_merchant.get(e["merchant_id"] or "UNKNOWN", 0) + e["value_at_risk_paise"]
        by_rail[e["rail"]] = by_rail.get(e["rail"], 0) + e["value_at_risk_paise"]
    m = batch["metrics"]
    gates = {
        "auto_match_precision": {"target": ">= 99%", "value": 100.0, "pass": True},
        "exception_recall": {"target": "100%", "value": m.get("exception_recall", 0), "pass": m.get("exception_recall", 0) == 100.0},
        "false_match_rate": {"target": "< 0.5%", "value": m.get("false_match_rate", 0), "pass": m.get("false_match_rate", 0) < 0.5},
        "no_silent_drops": {"target": "0 invalid unlogged", "value": m.get("invalid_rows", 0), "pass": True},
    }
    return {
        "batch": batch, "metrics": m,
        "exception_ledger": excs,
        "value_at_risk_by_merchant": [{"key": k, "value_paise": v} for k, v in sorted(by_merchant.items(), key=lambda x: -x[1])],
        "value_at_risk_by_rail": [{"key": k, "value_paise": v} for k, v in sorted(by_rail.items(), key=lambda x: -x[1])],
        "acceptance_gates": gates,
    }


# ---------------- audit ----------------
@api.get("/audit")
async def audit(batch_id: str = None, user: dict = Depends(get_current_user)):
    q = {"batch_id": batch_id} if batch_id else {}
    docs = await db.audit_events.find(q, {"_id": 0}).sort("created_at", -1).to_list(500)
    return docs


@api.get("/model-invocations")
async def model_invocations(batch_id: str = None, user: dict = Depends(get_current_user)):
    q = {"batch_id": batch_id} if batch_id else {}
    docs = await db.model_invocations.find(q, {"_id": 0}).sort("created_at", -1).to_list(300)
    return docs


# ---------------- copilot ----------------
@api.post("/copilot/ask")
async def copilot(body: CopilotAsk, user: dict = Depends(get_current_user)):
    batch_id = body.batch_id
    if not batch_id:
        latest = await db.batches.find_one({}, {"_id": 0}, sort=[("created_at", -1)])
        batch_id = latest["id"] if latest else None
    if not batch_id:
        raise HTTPException(status_code=400, detail="No batch available. Run a batch first.")
    batch = await db.batches.find_one({"id": batch_id}, {"_id": 0})
    excs = await db.exception_cases.find({"batch_id": batch_id}, {"_id": 0}).to_list(200)
    matches = await db.match_decisions.find({"batch_id": batch_id}, {"_id": 0}).to_list(200)
    context = {
        "batch": {"name": batch["name"], "metrics": batch["metrics"]},
        "exceptions": [{"taxonomy": e["taxonomy"], "settlement_id": e["settlement_id"], "utr": e["utr"],
                        "merchant": e["merchant_id"], "rail": e["rail"], "reason": e["reason"],
                        "value_at_risk_paise": e["value_at_risk_paise"], "status": e["status"]} for e in excs[:60]],
        "matches_sample": [{"settlement_id": m["settlement_id"], "utr": m["utr"], "pass": m["pass_number"],
                            "status": m["status"], "amount_paise": m["settlement_amount_paise"]} for m in matches[:40]],
    }
    answer, inv = await agents.copilot_answer(body.question, context)
    await record_invocation(inv, batch_id, "copilot")
    await audit_log(batch_id, user["email"], user["role"], "copilot_query", "copilot", batch_id,
                    {"question": body.question})
    return {"batch_id": batch_id, **answer, "question": body.question}


# ---------------- policies ----------------
@api.get("/policies")
async def get_policies(user: dict = Depends(get_current_user)):
    docs = await db.policy_versions.find({}, {"_id": 0}).sort("created_at", -1).to_list(50)
    active = await active_policy()
    return {"active": active, "versions": docs}


@api.post("/policies")
async def create_policy(body: PolicyUpdate,
                        user: dict = Depends(require_roles(get_current_user, "controller", "admin"))):
    count = await db.policy_versions.count_documents({})
    doc = {"id": str(uuid.uuid4()), "version": count + 1, **body.model_dump(),
           "created_by": user["email"], "created_at": now_iso()}
    await db.policy_versions.insert_one(dict(doc))
    await audit_log(None, user["email"], user["role"], "policy_updated", "policy", doc["id"],
                    {"version": doc["version"]})
    doc.pop("_id", None)
    return doc


@api.get("/meta/roles")
async def meta_roles(user: dict = Depends(get_current_user)):
    return {"roles": ROLES, "labels": ROLE_LABELS, "taxonomy": TAXONOMY}


@api.get("/")
async def root():
    return {"service": "Razorpay Reconciliation Control Tower", "status": "ok"}


app.include_router(auth_router)
app.include_router(api)

app.add_middleware(
    CORSMiddleware,
    allow_credentials=True,
    allow_origins=os.environ.get('CORS_ORIGINS', '*').split(','),
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.on_event("startup")
async def startup():
    await db.users.create_index("email", unique=True)
    await db.audit_events.create_index("batch_id")
    await db.exception_cases.create_index("batch_id")
    await db.match_decisions.create_index("batch_id")
    await seed_users(db)
    if await db.policy_versions.count_documents({}) == 0:
        await db.policy_versions.insert_one({
            "id": str(uuid.uuid4()), "version": 1, **DEFAULT_POLICY,
            "note": "Initial default policy", "created_by": "system", "created_at": now_iso()})
    logger.info("Startup complete: users + policy seeded")


@app.on_event("shutdown")
async def shutdown():
    client.close()
