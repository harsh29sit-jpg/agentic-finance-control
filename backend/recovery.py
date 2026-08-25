"""Bounded recovery orchestrator: detection -> intervention -> audited closure.

Design contract (the part a regulator would read):

  1. ELIGIBLE maps each exception taxonomy to exactly one bounded intervention.
     No free-form actions exist; the action space is closed.
  2. Every execution passes through GUARDS before anything happens:
       kill switch      -> recovery policy enabled flag
       attempt budget   -> max_attempts_per_case
       cool-off         -> hours since the previous attempt on the case
       daily value cap  -> sum(value of today's recovered+pending) vs cap
       maker-checker    -> value above require_checker_above_paise needs a
                           controller/admin, else lands in pending_approval
     A guard failure is not an error — it is an outcome ("blocked") recorded
     with the rule that fired.
  3. Outcomes are enumerable: recovered | pending_approval | blocked | failed.
  4. Every attempt appends to the tamper-evident audit chain AND a
     recovery_attempts document, so metrics are reconstructible offline.

Money stays integer paise throughout.
"""
import uuid
from datetime import datetime, timedelta, timezone

from services import audit_log

OUTCOMES = ("recovered", "pending_approval", "blocked", "failed")

DEFAULT_POLICY = {
    "enabled": True,
    "max_attempts_per_case": 3,
    "cool_off_hours": 24,
    "daily_value_cap_paise": 500_000_00,        # ₹5,00,000/day bounded exposure
    "require_checker_above_paise": 200_000,     # mirrors MATERIAL_THRESHOLD_PAISE
    "allowed_taxonomies": ["UNIDENTIFIED_CREDIT", "AMOUNT_MISMATCH",
                            "MISSING_IN_BANK", "TIMING_LAG"],
}


def _now():
    return datetime.now(timezone.utc)


def _iso(dt):
    return dt.isoformat()


async def get_policy(db):
    doc = await db.recovery_policies.find_one(sort=[("created_at", -1)],
                                              projection={"_id": 0})
    return doc or {**DEFAULT_POLICY, "version": 0}


async def set_policy(db, user, patch):
    cur = await get_policy(db)
    nxt = {**cur, **{k: patch[k] for k in DEFAULT_POLICY if k in patch}}
    nxt.pop("version", None)
    doc = {"id": str(uuid.uuid4()), "version": cur.get("version", 0) + 1,
           **nxt, "created_by": user["email"], "created_at": _iso(_now())}
    await db.recovery_policies.insert_one(dict(doc))
    await audit_log(db, None, user["email"], user["role"], "recovery_policy_updated",
                    "policy", doc["id"],
                    {k: nxt[k] for k in DEFAULT_POLICY if k in patch})
    return doc


# ------------------------------------------------------------------ proposals
def _evidence_link_orphan(case, candidates):
    """Unique amount+rail match among open MISSING_IN_BANK cases -> linkage."""
    amt = case["value_at_risk_paise"]
    hits = [c for c in candidates if abs(c["value_at_risk_paise"] - amt) <= 100]
    if len(hits) == 1:
        c = hits[0]
        return {"candidate_case_id": c["id"], "candidate_settlement_id":
                c.get("settlement_id"), "match_rule": "unique_amount_within_tolerance",
                "amount_paise": c["value_at_risk_paise"]}
    if len(hits) > 1:
        return {"ambiguous": True, "candidates": len(hits)}
    return None


def _evidence_fee_delta(case, tolerance_pct=0.10):
    """Delta consistent with a small fee/TDR residual (<=10% of gross)."""
    var = case["value_at_risk_paise"]
    gross = ((case.get("source_b") or {}).get("amount_paise")) or 0
    if var <= 0 or gross <= 0:
        return None
    ratio = var / gross
    if ratio <= tolerance_pct:
        return {"fee_ratio": round(ratio, 4), "delta_paise": var,
                "gross_paise": gross,
                "rule": f"delta<={int(tolerance_pct * 100)}%_of_gross"}
    return None


def _plan_for_case(case, policy, orphan_candidates, attempts_used):
    tax = case["taxonomy"]
    if tax not in tuple(policy["allowed_taxonomies"]):
        return None
    base = {"case_id": case["id"], "batch_id": case["batch_id"], "taxonomy": tax,
            "settlement_id": case.get("settlement_id"),
            "value_at_risk_paise": case["value_at_risk_paise"],
            "attempts_used": attempts_used}
    if tax == "UNIDENTIFIED_CREDIT":
        ev = _evidence_link_orphan(case, orphan_candidates)
        if ev is None:
            return {**base, "action": "link_orphan_credit", "proposable": False,
                    "reason": "no_unique_candidate"}
        if ev.get("ambiguous"):
            return {**base, "action": "link_orphan_credit", "proposable": False,
                    "reason": "ambiguous_candidates"}
        return {**base, "action": "link_orphan_credit", "proposable": True,
                "evidence": ev}
    if tax == "AMOUNT_MISMATCH":
        ev = _evidence_fee_delta(case)
        if not ev:
            return {**base, "action": "resolve_as_fee", "proposable": False,
                    "reason": "delta_not_fee_like"}
        return {**base, "action": "resolve_as_fee", "proposable": True,
                "evidence": ev}
    if tax == "MISSING_IN_BANK":
        return {**base, "action": "draft_bank_query", "proposable": True,
                "evidence": {"utr": case.get("utr"), "dedupe_window_hours":
                             policy["cool_off_hours"]}}
    if tax == "TIMING_LAG":
        return {**base, "action": "sla_watch_auto_clear", "proposable": True,
                "evidence": {"created_at": case.get("created_at")}}
    return None


async def build_plan(db, batch_id=None):
    """Non-mutating ranked plan across open eligible cases."""
    q = {"status": "open"}
    if batch_id:
        q["batch_id"] = batch_id
    cases = await db.exception_cases.find(q).sort(
        "value_at_risk_paise", -1).to_list(2000)
    policy = await get_policy(db)
    orphan_q = {"taxonomy": "MISSING_IN_BANK", "status": {"$in": ["open", "escalated"]}}
    if batch_id:
        orphan_q["batch_id"] = batch_id
    orphans = await db.exception_cases.find(orphan_q).to_list(500)
    plan, blocked = [], []
    for c in cases:
        used = await db.recovery_attempts.count_documents(
            {"case_id": c["id"]})
        item = _plan_for_case(c, policy, orphans, used)
        if item is None:
            continue
        (plan if item.get("proposable") else blocked).append(item)
    return {"plan": plan, "not_proposable": blocked, "policy_version":
            policy.get("version", 0)}


# ------------------------------------------------------------------ guards
async def _guard(db, policy, case, user):
    """Return (ok, rule) — rule names the stopping condition that fired."""
    if not policy.get("enabled"):
        return False, "kill_switch"
    tax = case["taxonomy"]
    if tax not in tuple(policy["allowed_taxonomies"]):
        return False, "taxonomy_not_allowed"
    used = await db.recovery_attempts.count_documents({"case_id": case["id"]})
    if used >= policy["max_attempts_per_case"]:
        return False, "max_attempts"
    last = await db.recovery_attempts.find_one(
        {"case_id": case["id"]}, sort=[("at", -1)])
    if last:
        lt = datetime.fromisoformat(last["at"])
        if _now() - lt < timedelta(hours=policy["cool_off_hours"]):
            return False, "cool_off"
    start = _now().replace(hour=0, minute=0, second=0, microsecond=0)
    rows = await db.recovery_attempts.aggregate([
        {"$match": {"at": {"$gte": _iso(start)},
                    "outcome": {"$in": ["recovered", "pending_approval"]}}},
        {"$group": {"_id": None, "v": {"$sum": "$value_at_risk_paise"}}},
    ]).to_list(1)
    spent = (rows[0]["v"] if rows else 0)
    if spent + case["value_at_risk_paise"] > policy["daily_value_cap_paise"]:
        return False, "daily_value_cap"
    if case["value_at_risk_paise"] > policy["require_checker_above_paise"] \
            and user["role"] not in ("controller", "admin"):
        return False, "pending_approval"       # routed, not rejected
    return True, None


# ------------------------------------------------------------------ actions
async def _act_link_orphan(db, user, case, evidence):
    target = await db.exception_cases.find_one({"id": evidence["candidate_case_id"]})
    if not target or target.get("status") not in ("open", "escalated"):
        return "failed", {"reason": "candidate_no_longer_open"}
    link = {"linked_case_id": target["id"],
            "linked_settlement_id": target.get("settlement_id"),
            "rule": evidence["match_rule"], "by": user["email"],
            "at": _iso(_now())}
    await db.exception_cases.update_one(
        {"id": case["id"]},
        {"$set": {"status": "resolved",
                  "review": {"action": "recovery_link", "by": user["email"],
                             "note": json_s(link)}, "recovery_link": link}})
    await db.exception_cases.update_one(
        {"id": target["id"]},
        {"$set": {"status": "resolved",
                  "review": {"action": "recovery_link_target", "by": user["email"],
                             "note": f"matched credit {case['id']}"}}})
    return "recovered", {"link": link}


async def _act_resolve_as_fee(db, user, case, evidence):
    review = {"action": "recovery_fee_resolution", "by": user["email"],
              "note": json_s(evidence)}
    await db.exception_cases.update_one(
        {"id": case["id"]}, {"$set": {"status": "resolved", "review": review,
                                      "fee_evidence": evidence}})
    return "recovered", {"fee_evidence": evidence}


async def _act_draft_bank_query(db, user, case, evidence):
    qref = f"BQ-{uuid.uuid4().hex[:10].upper()}"
    await db.exception_cases.update_one(
        {"id": case["id"]},
        {"$set": {"bank_query": {"ref": qref, "drafted_by": user["email"],
                                 "at": _iso(_now()), "utr": evidence["utr"]}}})
    return "pending_approval", {"bank_query_ref": qref,
                                "next": "await bank response within SLA"}


async def _act_sla_watch(db, user, case, evidence):
    created = case.get("created_at")
    if created:
        age_h = (_now() - datetime.fromisoformat(created)).total_seconds() / 3600
        if age_h >= 48:
            await db.exception_cases.update_one(
                {"id": case["id"]},
                {"$set": {"status": "resolved",
                          "review": {"action": "recovery_sla_autoclear",
                                     "by": user["email"],
                                     "note": f"aged {age_h:.0f}h beyond SLA"}}})
            return "recovered", {"aged_hours": round(age_h, 1)}
        return "blocked", {"reason": "within_sla_window",
                           "aged_hours": round(age_h, 1)}
    return "failed", {"reason": "missing_created_at"}


ACTIONS = {
    "link_orphan_credit": _act_link_orphan,
    "resolve_as_fee": _act_resolve_as_fee,
    "draft_bank_query": _act_draft_bank_query,
    "sla_watch_auto_clear": _act_sla_watch,
}


def json_s(obj):
    import json
    return json.dumps(obj, sort_keys=True)[:280]


# ------------------------------------------------------------------ execute
async def execute(db, user, case_ids, note=""):
    policy = await get_policy(db)
    plans = {}          # batch_id -> plan (built once per batch, not per case)
    results = []
    for cid in case_ids:
        case = await db.exception_cases.find_one({"id": cid})
        if not case or case.get("status") != "open":
            results.append({"case_id": cid, "outcome": "failed",
                            "detail": {"reason": "case_missing_or_not_open"}})
            continue
        ok, rule = await _guard(db, policy, case, user)
        if not ok:
            outcome = "pending_approval" if rule == "pending_approval" else "blocked"
            results.append({"case_id": cid, "outcome": outcome,
                            "detail": {"rule": rule}})
            if outcome == "pending_approval":
                await db.exception_cases.update_one(
                    {"id": cid},
                    {"$set": {"status": "pending_approval",
                              "review": {"action": "recovery_material_gate",
                                         "by": user["email"],
                                         "requires_approval": True}}})
            await _record_attempt(db, user, case, "gated", outcome,
                                  {"rule": rule}, results[-1])
            continue

        bid = case["batch_id"]
        if bid not in plans:
            plans[bid] = await build_plan(db, batch_id=bid)
        proposal = next((p for p in plans[bid]["plan"]
                         if p["case_id"] == cid), None)
        if not proposal:
            results.append({"case_id": cid, "outcome": "blocked",
                            "detail": {"rule": "no_valid_proposal"}})
            await _record_attempt(db, user, case, "gated", "blocked",
                                  {"rule": "no_valid_proposal"}, results[-1])
            continue

        try:
            outcome, detail = await ACTIONS[proposal["action"]](
                db, user, case, proposal.get("evidence", {}))
        except Exception as e:  # noqa: BLE001 — bounded failure is an outcome
            outcome, detail = "failed", {"error": str(e)[:200]}
        results.append({"case_id": cid, "outcome": outcome,
                        "action": proposal["action"], "detail": detail})
        await _record_attempt(db, user, case, proposal["action"], outcome,
                              detail, results[-1])
    recovered_value = sum(r["detail"].get("value_at_risk_paise", 0)
                          for r in results if r["outcome"] == "recovered")
    await audit_log(db, None, user["email"], user["role"], "recovery_executed",
                    "recovery_batch", str(uuid.uuid4())[:8],
                    {"requested": len(case_ids),
                     "outcomes": {o: sum(1 for r in results if r["outcome"] == o)
                                  for o in OUTCOMES},
                     "note": note})
    return {"results": results, "recovered_value_paise": recovered_value}


async def _record_attempt(db, user, case, action, outcome, detail, result_row):
    doc = {"id": str(uuid.uuid4()), "case_id": case["id"],
           "batch_id": case["batch_id"], "taxonomy": case["taxonomy"],
           "action": action, "outcome": outcome,
           "value_at_risk_paise": case["value_at_risk_paise"],
           "actor": user["email"], "role": user["role"],
           "at": _iso(_now()), "detail": detail}
    await db.recovery_attempts.insert_one(dict(doc))
    await audit_log(db, case["batch_id"], user["email"], user["role"],
                    f"recovery_{outcome}", "exception_case", case["id"],
                    {"action": action, "attempt_id": doc["id"]})
    result_row["detail"].setdefault("value_at_risk_paise",
                                    case["value_at_risk_paise"])
    return doc


# ------------------------------------------------------------------ metrics
async def metrics(db):
    pipeline_total = [
        {"$group": {"_id": {"taxonomy": "$taxonomy", "outcome": "$outcome"},
                    "value": {"$sum": "$value_at_risk_paise"},
                    "count": {"$sum": 1}}}]
    out = {"value_recovered_paise": 0, "value_pending_paise": 0,
           "value_blocked_paise": 0, "value_failed_paise": 0,
           "attempts": 0, "by_taxonomy": {}, "rule_hits": {}}
    async for row in db.recovery_attempts.aggregate(pipeline_total):
        tax, oc = row["_id"]["taxonomy"], row["_id"]["outcome"]
        out["attempts"] += row["count"]
        key = f"value_{oc}_paise"
        if key in out:
            out[key] += row["value"]
        slot = out["by_taxonomy"].setdefault(tax, {"attempts": 0, "recovered": 0})
        slot["attempts"] += row["count"]
        if oc == "recovered":
            slot["recovered"] += row["count"]
        if oc == "blocked" and isinstance(row.get("_id"), dict):
            pass
    async for r in db.recovery_attempts.find({"outcome": "blocked"},
                                            {"detail.rule": 1, "_id": 0}):
        rule = ((r.get("detail") or {}).get("rule")) or "unknown"
        out["rule_hits"][rule] = out["rule_hits"].get(rule, 0) + 1
    return out
