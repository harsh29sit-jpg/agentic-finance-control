"""Read-only tool registry for the agentic Copilot.

Every tool is a bounded, read-only query over reconciled data. Tools NEVER
mutate state — the agent can investigate and explain, but outcomes stay
human-gated elsewhere in the product.

Tool handlers receive (db, args, ctx):
  db   — AsyncIOMotorDatabase
  args — validated pydantic arg model
  ctx  — RunContext (default batch id, parsed attachments, policy, limits)

All money values cross this boundary as integer paise.
"""
from datetime import datetime
from typing import Optional, List, Literal

from pydantic import BaseModel, Field

MAX_ROWS_PER_TOOL = 25


# ---------------------------------------------------------------- context
class AttachmentContext:
    """One parsed user-supplied file, normalised for tool consumption."""

    def __init__(self, name, kind, columns, row_count, summary, rows=None):
        self.name = name
        self.kind = kind              # bank_statement | razorpay_settlements | razorpay_payments | ledger
        self.columns = columns
        self.row_count = row_count
        self.summary = summary        # dict of precomputed stats
        self.rows = rows or []        # canonical dicts (bank_statement kind only)


class RunContext(dict):
    """Bag passed to every tool: default_batch_id, attachments[], tolerance."""

    @property
    def attachments(self):
        return self.get("attachments", [])

    def find_attachment(self, name=None):
        atts = self.attachments
        if not atts:
            return None
        if name:
            for a in atts:
                if a.name == name:
                    return a
            return None
        return atts[0]


# ---------------------------------------------------------------- arg models
class QueryBatchesArgs(BaseModel):
    limit: int = Field(default=5, ge=1, le=20)


class QueryMatchesArgs(BaseModel):
    batch_id: Optional[str] = None
    settlement_id: Optional[str] = None
    utr: Optional[str] = None
    status: Optional[Literal["matched", "pending_review", "exception"]] = None
    pass_number: Optional[int] = Field(default=None, ge=1, le=3)
    min_amount_paise: Optional[int] = Field(default=None, ge=0)
    limit: int = Field(default=10, ge=1, le=MAX_ROWS_PER_TOOL)


class QueryExceptionsArgs(BaseModel):
    batch_id: Optional[str] = None
    taxonomy: Optional[str] = None
    merchant_id: Optional[str] = None
    rail: Optional[str] = None
    status: Optional[Literal["open", "escalated", "resolved", "rejected",
                             "pending_approval"]] = None
    min_value_at_risk_paise: Optional[int] = Field(default=None, ge=0)
    group_by: Optional[Literal["taxonomy", "merchant_id", "rail"]] = None
    limit: int = Field(default=10, ge=1, le=MAX_ROWS_PER_TOOL)


class SearchRecordsArgs(BaseModel):
    query: str = Field(min_length=2, max_length=80,
                       description="settlement_id / UTR / merchant substring")
    limit: int = Field(default=8, ge=1, le=MAX_ROWS_PER_TOOL)


class AggregateArgs(BaseModel):
    collection: Literal["matches", "exceptions"]
    op: Literal["count", "sum", "avg"] = "count"
    field: Optional[Literal["settlement_amount_paise", "value_at_risk_paise"]] = None
    batch_id: Optional[str] = None
    taxonomy: Optional[str] = None
    merchant_id: Optional[str] = None
    status: Optional[str] = None
    group_by: Optional[Literal["taxonomy", "merchant_id", "rail", "status"]] = None


class BatchReportArgs(BaseModel):
    batch_id: Optional[str] = None


class RunBenchmarkArgs(BaseModel):
    batch_id: Optional[str] = None


class AuditTimelineArgs(BaseModel):
    batch_id: Optional[str] = None
    entity_id: Optional[str] = None
    limit: int = Field(default=10, ge=1, le=30)


class PreviewReconcileArgs(BaseModel):
    attachment_name: Optional[str] = None
    batch_id: Optional[str] = None


# ---------------------------------------------------------------- helpers
def _rupees(paise):
    return round((paise or 0) / 100, 2)


def _default_batch(db, ctx):
    return ctx.get("default_batch_id")


async def _resolve_batch(db, ctx, explicit=None):
    bid = explicit or _default_batch(db, ctx)
    if not bid:
        return None, None
    b = await db.batches.find_one({"id": bid}, {"_id": 0})
    return bid, b


def _match_row(m):
    return {
        "settlement_id": m.get("settlement_id"), "utr": m.get("utr"),
        "merchant_id": m.get("merchant_id"), "rail": m.get("rail"),
        "status": m.get("status"), "pass": m.get("pass_number"),
        "confidence": m.get("confidence"),
        "amount_paise": m.get("settlement_amount_paise"),
        "tolerance_paise": m.get("tolerance_paise"),
        "date_gap_days": m.get("date_gap_days"), "note": m.get("note"),
    }


def _exception_row(e):
    return {
        "id": e.get("id"), "taxonomy": e.get("taxonomy"),
        "settlement_id": e.get("settlement_id"), "utr": e.get("utr"),
        "merchant_id": e.get("merchant_id"), "rail": e.get("rail"),
        "status": e.get("status"),
        "value_at_risk_paise": e.get("value_at_risk_paise"),
        "reason": e.get("reason"),
        "triage_action": (e.get("triage") or {}).get("suggested_action"),
    }


# ---------------------------------------------------------------- tools
async def t_query_batches(db, a: QueryBatchesArgs, ctx):
    docs = await db.batches.find(
        {}, {"_id": 0, "id": 1, "name": 1, "created_at": 1, "sandbox": 1,
             "counts": 1, "source_label": 1,
             "m.deterministic_match_rate": 1, "m.inclusive_match_rate": 1,
             "m.value_at_risk_paise": 1, "m.open_exceptions": 1}
    ).sort("created_at", -1).limit(a.limit).to_list(a.limit)
    for d in docs:
        m = d.pop("m", {}) or {}
        d["deterministic_match_rate"] = m.get("deterministic_match_rate")
        d["inclusive_match_rate"] = m.get("inclusive_match_rate")
        d["value_at_risk_paise"] = m.get("value_at_risk_paise")
        d["open_exceptions"] = m.get("open_exceptions")
    return {"batches": docs}


async def t_query_matches(db, a: QueryMatchesArgs, ctx):
    q = {}
    bid = a.batch_id or _default_batch(db, ctx)
    if bid:
        q["batch_id"] = bid
    if a.settlement_id:
        q["settlement_id"] = {"$regex": a.settlement_id, "$options": "i"}
    if a.utr:
        q["utr"] = {"$regex": a.utr, "$options": "i"}
    if a.status:
        q["status"] = a.status
    if a.pass_number:
        q["pass_number"] = a.pass_number
    if a.min_amount_paise is not None:
        q["settlement_amount_paise"] = {"$gte": a.min_amount_paise}
    total = await db.match_decisions.count_documents(q)
    docs = await db.match_decisions.find(q, {"_id": 0}) \
        .sort("settlement_amount_paise", -1).limit(a.limit).to_list(a.limit)
    return {"total_matching": total, "batch_id": bid,
            "matches": [_match_row(m) for m in docs]}


async def t_query_exceptions(db, a: QueryExceptionsArgs, ctx):
    q = {}
    bid = a.batch_id or _default_batch(db, ctx)
    if bid:
        q["batch_id"] = bid
    for f in ("taxonomy", "merchant_id", "rail", "status"):
        v = getattr(a, f)
        if v:
            q[f] = v
    if a.merchant_id:
        q["merchant_id"] = {"$regex": a.merchant_id, "$options": "i"}
    if a.min_value_at_risk_paise is not None:
        q["value_at_risk_paise"] = {"$gte": a.min_value_at_risk_paise}

    total = await db.exception_cases.count_documents(q)

    if a.group_by:
        cursor = db.exception_cases.aggregate([
            {"$match": q},
            {"$group": {"_id": f"${a.group_by}", "count": {"$sum": 1},
                        "value_at_risk_paise": {"$sum": "$value_at_risk_paise"}}},
            {"$sort": {"value_at_risk_paise": -1}},
            {"$limit": a.limit},
            {"$project": {"_id": 0, a.group_by: "$_id", "count": 1,
                          "value_at_risk_paise": 1}},
        ])
        groups = [g async for g in cursor]
        for g in groups:
            g["value_rupees"] = _rupees(g["value_at_risk_paise"])
        return {"total_matching": total, "group_by": a.group_by,
                "batch_id": bid, "groups": groups}

    docs = await db.exception_cases.find(q, {"_id": 0}) \
        .sort("value_at_risk_paise", -1).limit(a.limit).to_list(a.limit)
    return {"total_matching": total, "batch_id": bid,
            "exceptions": [_exception_row(e) for e in docs],
            "total_value_at_risk_paise": sum(e["value_at_risk_paise"] for e in docs)}


async def t_search_records(db, a: SearchRecordsArgs, ctx):
    rx = {"$regex": a.query, "$options": "i"}
    matches = await db.match_decisions.find(
        {"$or": [{"settlement_id": rx}, {"utr": rx}, {"merchant_id": rx}]},
        {"_id": 0}).sort("settlement_amount_paise", -1).limit(a.limit).to_list(a.limit)
    excs = await db.exception_cases.find(
        {"$or": [{"settlement_id": rx}, {"utr": rx}, {"merchant_id": rx},
                 {"reason": rx}]},
        {"_id": 0}).sort("value_at_risk_paise", -1).limit(a.limit).to_list(a.limit)
    return {"query": a.query,
            "matches": [_match_row(m) for m in matches[:a.limit]],
            "exceptions": [_exception_row(e) for e in excs[:a.limit]]}


async def t_aggregate(db, a: AggregateArgs, ctx):
    coll = db.match_decisions if a.collection == "matches" else db.exception_cases
    q = {}
    bid = a.batch_id or _default_batch(db, ctx)
    if bid:
        q["batch_id"] = bid
    for f in ("taxonomy", "merchant_id", "status"):
        v = getattr(a, f)
        if v:
            q[f] = v
    field = a.field or ("settlement_amount_paise" if a.collection == "matches"
                        else "value_at_risk_paise")

    if a.op == "count" and not a.group_by:
        return {"collection": a.collection, "op": "count", "filters": q,
                "value": await coll.count_documents(q)}

    pipeline = [{"$match": q}]
    if a.group_by:
        pipeline += [
            {"$group": {"_id": f"${a.group_by}",
                        "n": {"$sum": 1},
                        "value": {"$sum": f"${field}" if a.op != "count" else 1}}},
            {"$sort": {"value": -1}}, {"$limit": MAX_ROWS_PER_TOOL},
            {"$project": {"_id": 0, a.group_by: "$_id", "n": 1, "value": 1}},
        ]
        groups = [g async for g in coll.aggregate(pipeline)]
        return {"collection": a.collection, "op": a.op, "field": field,
                "groups": groups}
    pipeline += [{"$group": {"_id": None, "value": {
        "$sum": f"${field}" if a.op == "sum" else
        {"$avg": f"${field}"}}}}]
    res = await coll.aggregate(pipeline).to_list(1)
    value = round(res[0]["value"]) if res and res[0].get("value") is not None else 0
    return {"collection": a.collection, "op": a.op, "field": field,
            "filters": q, "value_paise": int(value), "value_rupees": _rupees(value)}


async def t_batch_report(db, a: BatchReportArgs, ctx):
    bid, b = await _resolve_batch(db, ctx, a.batch_id)
    if not b:
        return {"error": "no batch available"}
    excs = await db.exception_cases.find(
        {"batch_id": bid}, {"_id": 0, "merchant_id": 1, "rail": 1,
                            "value_at_risk_paise": 1}).to_list(2000)
    by_merchant, by_rail = {}, {}
    for e in excs:
        mk = e.get("merchant_id") or "UNKNOWN"
        by_merchant[mk] = by_merchant.get(mk, 0) + e["value_at_risk_paise"]
        by_rail[e.get("rail")] = by_rail.get(e.get("rail"), 0) + e["value_at_risk_paise"]
    m = b.get("metrics", {})
    return {
        "batch": {"id": bid, "name": b.get("name"),
                  "sandbox": b.get("sandbox", False),
                  "policy_version": b.get("policy_version")},
        "metrics": {"total_settlements": m.get("total_settlements"),
                    "pass1": m.get("pass1_matches"), "pass2": m.get("pass2_matches"),
                    "auto_matched": m.get("auto_matched"),
                    "reconciled_value_paise": m.get("reconciled_value_paise"),
                    "value_at_risk_paise": m.get("value_at_risk_paise"),
                    "open_exceptions": m.get("open_exceptions")},
        "top_merchants_by_var": [{"merchant": k, "value_at_risk_paise": v}
                                 for k, v in sorted(by_merchant.items(),
                                                    key=lambda x: -x[1])[:5]],
        "var_by_rail": [{"rail": k, "value_at_risk_paise": v}
                        for k, v in sorted(by_rail.items(), key=lambda x: -x[1])[:6]],
    }


async def t_run_benchmark(db, a: RunBenchmarkArgs, ctx):
    from engine import compute_benchmark
    bid, b = await _resolve_batch(db, ctx, a.batch_id)
    if not b:
        return {"error": "no batch available"}
    truth = b.get("truth") or []
    if not truth:
        return {"has_truth": False,
                "message": f"Batch {bid[:8]} has no labelled truth set "
                           "(uploaded batches are unlabelled)."}
    proj = {"_id": 0, "settlement_id": 1, "utr": 1, "status": 1}
    matches = await db.match_decisions.find({"batch_id": bid}, proj).to_list(5000)
    excs = await db.exception_cases.find({"batch_id": bid},
                                         {"_id": 0, "settlement_id": 1, "utr": 1}).to_list(5000)
    score = compute_benchmark(truth, matches, excs)
    return {"has_truth": True, "batch_id": bid, "batch_name": b.get("name"),
            "precision": score["auto_match_precision"],
            "recall": score["match_recall"],
            "exception_recall": score["exception_recall"],
            "f1": score["f1_score"],
            "false_match_rate": score["false_match_rate"],
            "confusion": {"tp": score["true_positive"], "fp": score["false_positive"],
                          "fn": score["false_negative"]}}


async def t_audit_timeline(db, a: AuditTimelineArgs, ctx):
    q = {}
    if a.batch_id or _default_batch(db, ctx):
        q["batch_id"] = a.batch_id or _default_batch(db, ctx)
    if a.entity_id:
        q["$or"] = [{"entity_id": a.entity_id}, {"details.entity_id": a.entity_id}]
    docs = await db.audit_events.find(
        q, {"_id": 0, "seq": 1, "actor": 1, "role": 1, "action": 1,
            "entity": 1, "entity_id": 1, "created_at": 1, "details": 1}
    ).sort("seq", -1).limit(a.limit).to_list(a.limit)
    return {"events": docs}


async def t_preview_reconcile(db, a: PreviewReconcileArgs, ctx):
    """Match an attached bank-statement file against a batch's settlements."""
    att = ctx.find_attachment(a.attachment_name)
    if not att:
        return {"error": "no bank-statement attachment found in this request"}
    if att.kind != "bank_statement":
        return {"error": f"attachment {att.name!r} is a {att.kind} file; "
                         "preview_reconcile needs a bank-statement CSV"}

    bid, b = await _resolve_batch(db, ctx, a.batch_id)
    if not b:
        return {"error": "no batch available to reconcile against"}

    settlements = await db.match_decisions.find(
        {"batch_id": bid}, {"_id": 0, "settlement_id": 1, "utr": 1,
                            "settlement_amount_paise": 1}).to_list(5000)
    by_utr = {s["utr"]: s for s in settlements if s.get("utr")}
    tolerance = int(ctx.get("tolerance_paise", 100))

    matched, fuzzy, unmatched = [], [], []
    stmt_value = 0
    used_utrs = set()
    for r in att.rows[:5000]:
        amt = r.get("amount_paise") or 0
        stmt_value += amt
        s = by_utr.get(r.get("utr"))
        if s and s["utr"] not in used_utrs:
            used_utrs.add(s["utr"])
            matched.append({"statement_utr": r.get("utr"),
                            "statement_amount_paise": amt,
                            "settlement_id": s["settlement_id"],
                            "delta_paise": amt - s["settlement_amount_paise"]})
            continue
        cands = [x for x in settlements
                 if x.get("utr") not in used_utrs
                 and abs(x["settlement_amount_paise"] - amt) <= tolerance]
        if len(cands) == 1:
            used_utrs.add(cands[0]["utr"])
            fuzzy.append({"statement_amount_paise": amt,
                          "settlement_id": cands[0]["settlement_id"],
                          "note": "amount within tolerance, UTR absent/truncated"})
        else:
            unmatched.append({"statement_utr": r.get("utr") or "",
                              "amount_paise": amt})

    orphan = [s for s in settlements if s["utr"] not in used_utrs]
    return {
        "attachment": att.name, "batch_id": bid, "batch_name": b.get("name"),
        "statement_rows": min(len(att.rows), 5000),
        "statement_total_paise": stmt_value, "statement_total_rupees": _rupees(stmt_value),
        "matched_by_utr": len(matched), "matched_fuzzy": len(fuzzy),
        "unmatched_credits": len(unmatched),
        "unmatched_value_paise": sum(u["amount_paise"] for u in unmatched),
        "batch_settlements_not_on_statement": len(orphan),
        "samples": {"matched": matched[:3], "fuzzy": fuzzy[:2],
                    "unmatched": unmatched[:5]},
    }


# ---------------------------------------------------------------- registry
TOOLS = {
    "query_batches": (t_query_batches, QueryBatchesArgs,
                      "List recent reconciliation batches with match rates and value at risk."),
    "query_matches": (t_query_matches, QueryMatchesArgs,
                      "Fetch settled/matched decisions filtered by settlement_id, UTR, status, pass number or minimum amount."),
    "query_exceptions": (t_query_exceptions, QueryExceptionsArgs,
                         "Fetch or group exception cases by taxonomy/merchant/rail/status with value-at-risk sorting."),
    "search_records": (t_search_records, SearchRecordsArgs,
                       "Free-text lookup of a specific settlement_id / UTR / merchant across matches and exceptions."),
    "aggregate": (t_aggregate, AggregateArgs,
                  "Count / sum / average amounts over matches or exceptions, optionally grouped."),
    "batch_report": (t_batch_report, BatchReportArgs,
                     "Full report for one batch: metrics, acceptance-relevant numbers, top merchants by value at risk."),
    "run_benchmark": (t_run_benchmark, RunBenchmarkArgs,
                      "Precision / recall / F1 evaluation of a batch against its labelled truth set."),
    "audit_timeline": (t_audit_timeline, AuditTimelineArgs,
                       "Recent immutable audit events for a batch or entity."),
    "preview_reconcile": (t_preview_reconcile, PreviewReconcileArgs,
                          "Reconcile an attached bank-statement file against a batch's settlements (UTR-exact then tolerance-fuzzy)."),
}


def tool_catalog_for_prompt():
    lines = []
    for name, (_fn, argm, desc) in TOOLS.items():
        params = ", ".join(f"{f}({v.annotation.__name__ if hasattr(v.annotation,'__name__') else 'any'})"
                           for f, v in argm.model_fields.items())
        lines.append(f"- {name}: {desc} Args: {{{params}}}")
    return "\n".join(lines)
