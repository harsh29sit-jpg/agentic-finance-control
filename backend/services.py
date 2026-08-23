"""Shared business services — the single execution path for state-changing ops.

Both HTTP routes and agent action-tools call these functions, so RBAC,
maker-checker policy and audit logging behave identically no matter who
pulls the trigger (a human in the UI or an agent on a user's behalf).

Every function takes an explicit `db` handle and a `user` dict
({email, role}) and enforces authorization itself. Money stays integer paise.
"""
import hashlib
import json
import uuid
import logging
from datetime import datetime, timezone

from pymongo.errors import BulkWriteError, DuplicateKeyError

logger = logging.getLogger("recon.services")

GENESIS_HASH = "0" * 64
AUDIT_MAX_RETRIES = 25


def _utcnow_iso():
    return datetime.now(timezone.utc).isoformat()


# ------------------------------------------------------------------ audit chain
def _hash_event(prev_hash, payload):
    canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256((prev_hash + canonical).encode()).hexdigest()


def _build_event_payload(batch_id, actor, role, action, entity, entity_id, details):
    return {
        "id": str(uuid.uuid4()), "batch_id": batch_id, "actor": actor, "role": role,
        "action": action, "entity": entity, "entity_id": entity_id,
        "details": details or {}, "created_at": _utcnow_iso(),
    }


async def append_audit_events(coll, payload_specs):
    """Append events to the tamper-evident hash chain — safe across instances.

    Coordination primitive: unique index on `seq`. Concurrent writers race;
    losers observe duplicate-key failures, re-read the head, rebuild, retry.
    """
    if not payload_specs:
        return []
    for _ in range(AUDIT_MAX_RETRIES):
        head = await coll.find_one(sort=[("seq", -1)]) or {"seq": 0, "hash": GENESIS_HASH}
        seq, prev, built = head["seq"], head["hash"], []
        for spec in payload_specs:
            payload = _build_event_payload(**spec)
            h = _hash_event(prev, payload)
            built.append({**payload, "seq": seq + 1, "prev_hash": prev, "hash": h})
            seq += 1
            prev = h
        try:
            await coll.insert_many([dict(e) for e in built], ordered=True)
            return built
        except BulkWriteError as bwe:
            landed = bwe.details.get("writeErrors", [{}])[0].get("index", 0)
            if landed > 0:
                payload_specs = payload_specs[landed:]
            continue
        except DuplicateKeyError:
            continue
    raise RuntimeError("audit chain contention: could not append after retries")


async def audit_log(db, batch_id, actor, role, action, entity, entity_id, details=None):
    evs = await append_audit_events(db.audit_events, [
        {"batch_id": batch_id, "actor": actor, "role": role, "action": action,
         "entity": entity, "entity_id": entity_id, "details": details}])
    return evs[0]


async def record_invocation(db, inv, batch_id, entity_id):
    await db.model_invocations.insert_one({**inv, "batch_id": batch_id,
                                           "entity_id": entity_id})


# ------------------------------------------------------------------ policies
DEFAULT_POLICY = {"amount_tolerance_paise": 100, "timing_lag_days": 1,
                  "auto_post_confidence": 0.95}


async def active_policy(db):
    doc = await db.policy_versions.find_one(sort=[("created_at", -1)],
                                            projection={"_id": 0})
    if not doc:
        return {**DEFAULT_POLICY, "version": 1}
    return doc


async def create_policy_version(db, user, amount_tolerance_paise, timing_lag_days,
                                auto_post_confidence, note=""):
    count = await db.policy_versions.count_documents({})
    doc = {"id": str(uuid.uuid4()), "version": count + 1,
           "amount_tolerance_paise": int(amount_tolerance_paise),
           "timing_lag_days": int(timing_lag_days),
           "auto_post_confidence": float(auto_post_confidence),
           "note": note or "", "created_by": user["email"],
           "created_at": _utcnow_iso()}
    await db.policy_versions.insert_one(dict(doc))
    await audit_log(db, None, user["email"], user["role"], "policy_updated",
                    "policy", doc["id"], {"version": doc["version"]})
    return doc


# ------------------------------------------------------------------ reviews
MATCH_ACTION_STATUS = {"approve": "matched", "reject": "exception",
                       "escalate": "pending_review"}
EXC_ACTION_STATUS = {"resolve": "resolved", "escalate": "escalated",
                     "override": "resolved", "reject": "rejected"}
MATERIAL_THRESHOLD_PAISE = 200000


async def review_match(db, user, decision_id, action, note=""):
    """Workbench match-decision review. action: approve|reject|escalate."""
    doc = await db.match_decisions.find_one({"id": decision_id})
    if not doc:
        raise LookupError("Match decision not found")
    new_status = MATCH_ACTION_STATUS[action]
    await db.match_decisions.update_one({"id": decision_id}, {"$set": {
        "status": new_status, "reviewed_by": user["email"], "review_note": note}})
    await audit_log(db, doc["batch_id"], user["email"], user["role"],
                    f"match_{action}", "match_decision", decision_id,
                    {"note": note, "utr": doc.get("utr")})
    return {"id": decision_id, "status": new_status}


async def review_exception(db, user, case_id, action, note=""):
    """Exception-case review. action: resolve|escalate|override|reject.
    Maker-checker: overrides on material value need controller/admin."""
    if action not in EXC_ACTION_STATUS:
        raise ValueError("Invalid action")
    # accept internal id OR human-facing settlement_id (agent + UI parity)
    case = await db.exception_cases.find_one({"id": case_id}) \
        or await db.exception_cases.find_one(
            {"settlement_id": case_id.upper(), "status": {"$in": ["open", "escalated"]}})
    if not case:
        raise LookupError("Exception not found")
    case_id = case["id"]
    material = case["value_at_risk_paise"] > MATERIAL_THRESHOLD_PAISE
    if action == "override" and material and user["role"] not in ("controller", "admin"):
        status = "pending_approval"
    else:
        status = EXC_ACTION_STATUS[action]
    review = {"action": action, "by": user["email"], "role": user["role"],
              "note": note, "at": _utcnow_iso(),
              "requires_approval": status == "pending_approval"}
    await db.exception_cases.update_one({"id": case_id},
                                        {"$set": {"status": status, "review": review}})
    await audit_log(db, case["batch_id"], user["email"], user["role"],
                    f"exception_{action}", "exception_case", case_id,
                    {"note": note, "resulting_status": status})
    return {"id": case_id, "status": status, "requires_approval":
            status == "pending_approval"}


async def decide_override(db, user, case_id, approve, note=""):
    """Checker sign-off on a pending material override (controller/admin only)."""
    if user["role"] not in ("controller", "admin"):
        raise PermissionError("Only controller/admin can decide overrides")
    case = await db.exception_cases.find_one({"id": case_id}) \
        or await db.exception_cases.find_one({"settlement_id": case_id.upper()})
    if not case:
        raise LookupError("Exception not found")
    case_id = case["id"]
    if case.get("status") != "pending_approval":
        raise ValueError("Case is not pending approval")
    status = "resolved" if approve else "open"
    approval = {"approved": approve, "by": user["email"], "role": user["role"],
                "note": note, "at": _utcnow_iso()}
    await db.exception_cases.update_one({"id": case_id},
                                        {"$set": {"status": status, "approval": approval}})
    await audit_log(db, case["batch_id"], user["email"], user["role"],
                    "override_" + ("approved" if approve else "rejected"),
                    "exception_case", case_id, {"note": note})
    return {"id": case_id, "status": status}


async def bulk_review_exceptions(db, user, batch_id, action, taxonomy=None,
                                 ids=None, note=""):
    if action not in ("resolve", "escalate", "reject"):
        raise ValueError("Invalid bulk action")
    q = {"batch_id": batch_id, "status": {"$in": ["open", "escalated"]}}
    if taxonomy:
        q["taxonomy"] = taxonomy
    if ids:
        q["id"] = {"$in": ids}
    cases = await db.exception_cases.find(q, {"_id": 0}).to_list(2000)
    status = {"resolve": "resolved", "escalate": "escalated", "reject": "rejected"}[action]
    review = {"action": action, "by": user["email"], "role": user["role"],
              "note": note, "at": _utcnow_iso(), "bulk": True}
    affected = [c["id"] for c in cases]
    if affected:
        await db.exception_cases.update_many({"id": {"$in": affected}},
                                             {"$set": {"status": status,
                                                       "review": review}})
    await audit_log(db, batch_id, user["email"], user["role"], f"bulk_{action}",
                    "exception_case", taxonomy or "selection",
                    {"count": len(affected), "taxonomy": taxonomy, "note": note})
    return {"affected": len(affected), "status": status}


# ------------------------------------------------------------------ batches
async def create_sandbox_batch(db, user, process_batch_fn, generate_fn):
    """Truth-labelled synthetic fixture. Sandbox data never counts toward
    production dashboards by default."""
    seq = await db.batches.count_documents({})
    seed = 42 + seq
    rows, truth = generate_fn(seed=seed)
    return await process_batch_fn(
        f"Sandbox Fixture #{seq + 1}", rows, user["email"], user["role"],
        "sandbox:seed", truth=truth, rerun_seed=seed, sandbox=True)


async def rerun_batch(db, user, batch_id, process_batch_fn):
    base = await db.batches.find_one({"id": batch_id}, {"_id": 0})
    if not base:
        raise LookupError("Batch not found")
    root_id = base.get("parent_batch_id") or batch_id
    n_reruns = await db.batches.count_documents({"parent_batch_id": root_id})
    from seed_data import generate_batch
    if base.get("rerun_seed") is not None:
        rows, truth = generate_batch(seed=base["rerun_seed"])
        rerun_seed = base["rerun_seed"]
    else:
        cur = db.raw_files.find({"batch_id": batch_id}).sort("seq", 1)
        rows, truth, rerun_seed = [], [], None
        async for ch in cur:
            rows.extend(ch.get("rows", []))
        if not rows:
            raise ValueError("Original source rows unavailable for rerun")
    new = await process_batch_fn(
        f"{base['name']} (rerun {n_reruns + 1})", rows, user["email"], user["role"],
        base["source_label"], truth=truth, rerun_seed=rerun_seed,
        parent_batch_id=root_id, sandbox=bool(base.get("sandbox")))
    await audit_log(db, new["id"], user["email"], user["role"], "batch_rerun",
                    "batch", new["id"], {"parent_batch_id": root_id,
                                         "policy_version": new["policy_version"]})
    return new


# ------------------------------------------------------------------ schedules
async def create_schedule(db, user, name, cron, action, enabled=True, note="",
                          next_fire_fn=None):
    from scheduler import CronError
    nxt = next_fire_fn(cron, datetime.now(timezone.utc))
    doc = {"id": str(uuid.uuid4()), "name": name, "cron": cron, "action": action,
           "enabled": enabled, "note": note or "", "created_by": user["email"],
           "created_at": _utcnow_iso(),
           "next_run_at": nxt.isoformat() if enabled else None,
           "last_run_at": None, "last_status": None, "last_result": {},
           "run_count": 0, "in_flight": False}
    await db.schedules.insert_one(dict(doc))
    await audit_log(db, None, user["email"], user["role"], "schedule_created",
                    "schedule", doc["id"], {"cron": cron, "action": action})
    return doc
