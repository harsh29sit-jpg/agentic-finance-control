#!/usr/bin/env python3
"""Index coverage report at production cardinality.

Samples REAL values from the database (largest batch, live taxonomies...),
then EXPLAINs every hot query shape and flags COLLSCANs / doc-examined blowups.

Usage:
  .venv/bin/python scripts/index_report.py            # report only
  .venv/bin/python scripts/index_report.py --apply    # also ensure recommended
"""
import argparse
import asyncio
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "backend"))

from motor.motor_asyncio import AsyncIOMotorClient

RECOMMENDED = {
    "match_decisions": [
        [("batch_id", 1), ("status", 1), ("settlement_amount_paise", -1)],
        [("batch_id", 1), ("settlement_amount_paise", -1)],
        [("id", 1)],
    ],
    "exception_cases": [
        [("batch_id", 1), ("status", 1), ("value_at_risk_paise", -1)],
        [("batch_id", 1), ("taxonomy", 1), ("value_at_risk_paise", -1)],
        [("value_at_risk_paise", -1)],
    ],
    "batches": [
        [("created_at", -1)],
        [("source_fingerprint", 1)],
        [("sandbox", 1), ("created_at", -1)],
    ],
    "audit_events": [
        [("batch_id", 1), ("created_at", -1)],
    ],
}


def _explain_flags(stats, plan):
    stage = plan.get("stage", "")
    while stage in ("PROJECTION", "PROJECTION_SIMPLE", "LIMIT",
                    "FETCH", "SORT_WITH_LIMIT", "SORT"):
        inner = plan.get("inputStage")
        if not inner:
            break
        stage = inner.get("stage", "")
        plan = inner
    sort_bad = "SORT" in stage  # in-memory sort = missing compound index
    scan = stage == "COLLSCAN"
    return scan or sort_bad or stats.get("totalDocsExamined", 0) > \
        max(stats.get("nReturned", 0) * 50, 2000)


async def run_explain(db, coll_name, flt, sort):
    inner = {"find": coll_name, "filter": flt}
    if sort:
        inner["sort"] = dict(sort)
    inner["limit"] = 25
    e = await db.command({"explain": inner, "verbosity": "executionStats"})
    stats = e.get("executionStats", {})
    plan = e.get("queryPlanner", {}).get("winningPlan", {})
    return {
        "stage": plan.get("stage", "?"),
        "keys": stats.get("totalKeysExamined", 0),
        "docs": stats.get("totalDocsExamined", 0),
        "ret": stats.get("nReturned", 0),
        "ms": stats.get("executionTimeMillis", 0),
        "bad": _explain_flags(stats, plan),
    }


async def main(apply):
    uri = os.environ.get("MONGO_URL", "mongodb://localhost:27017")
    db = AsyncIOMotorClient(uri)[os.environ.get("DB_NAME", "recon_control_tower")]

    # ---- realistic samples at current cardinality ----
    biggest = await (db.match_decisions.aggregate([
        {"$group": {"_id": "$batch_id", "n": {"$sum": 1}}},
        {"$sort": {"n": -1}}, {"$limit": 1}])).to_list(1)
    bid = biggest[0]["_id"] if biggest else None
    tax_doc = await db.exception_cases.find_one(
        {"batch_id": bid}, {"taxonomy": 1})
    taxonomy = (tax_doc or {}).get("taxonomy") or "DUPLICATE"

    queries = []  # (coll, filter_dict, sort, purpose) — filters mutable below
    if bid:
        queries += [
            ("match_decisions", {"batch_id": bid, "status": "matched"},
             [("settlement_amount_paise", -1)], "workbench filtered"),
            ("match_decisions", {"batch_id": bid},
             [("settlement_amount_paise", -1)], "workbench full batch"),
            ("exception_cases", {"batch_id": bid, "status": "open"},
             [("value_at_risk_paise", -1)], "exception command center"),
            ("exception_cases",
             {"batch_id": bid, "taxonomy": taxonomy},
             [("value_at_risk_paise", -1)], "taxonomy group view"),
            ("model_invocations", {"batch_id": bid},
             [("created_at", -1)], "invocation log"),
            ("raw_files", {"batch_id": bid}, [("seq", 1)], "rerun replay"),
            ("audit_events", {"batch_id": bid}, [("created_at", -1)],
             "audit console"),
        ]
    queries += [
        ("batches", {}, [("created_at", -1)], "batch list / trend"),
        ("exception_cases", {}, [("value_at_risk_paise", -1)], "global VAR sort"),
        ("exception_cases", {"status": "pending_approval"}, None, "checker queue"),
    ]
    any_decision = await db.match_decisions.find_one({}, {"id": 1})
    if any_decision:
        queries.append(("match_decisions", {"id": any_decision["id"]}, None,
                        "decision detail"))

    print(f"sampled batch={bid} taxonomy={taxonomy}")
    print(f"{'coll':<18} {'purpose':<26} {'plan':<10} {'keys':>7} "
          f"{'docs':>8} {'ret':>6} {'ms':>5}  flag")
    print("-" * 100)

    flagged = set()
    for coll_name, flt, sort, purpose in queries:
        r = await run_explain(db, coll_name, flt, sort)
        flag = " ⚠ NEEDS INDEX" if r["bad"] else ""
        if r["bad"]:
            flagged.add(coll_name)
        print(f"{coll_name:<18} {purpose:<26} {r['stage']:<10} "
              f"{r['keys']:>7} {r['docs']:>8} {r['ret']:>6} {r['ms']:>4}{flag}")

    if flagged:
        print("\nrecommended for flagged collections:")
        for c in sorted(flagged):
            for ix in RECOMMENDED.get(c, []):
                print(f"  {c}: {ix}")
        if apply:
            print("\nensuring indexes…")
            for c in sorted(flagged):
                for ix in RECOMMENDED.get(c, []):
                    name = await db[c].create_index(ix)
                    print(f"  ✓ {c}.{name}")
    else:
        print("\nall hot query shapes are index-covered ✓")


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    asyncio.run(main("--apply" in sys.argv))
