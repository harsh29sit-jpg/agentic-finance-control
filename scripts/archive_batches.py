#!/usr/bin/env python3
"""Batch archival — keeps the hot database lean without losing history.

Moves batches older than N days (with their decisions/exceptions/audit
events/raw files) from the hot collections into `archive_*` siblings.
Idempotent per batch; `--restore` puts everything back exactly.

Usage:
  .venv/bin/python scripts/archive_batches.py --days 90 --dry-run
  .venv/bin/python scripts/archive_batches.py --days 90
  .venv/bin/python scripts/archive_batches.py --batch-id <id> --restore

Collections moved per batch: batches, match_decisions, exception_cases,
raw_files, model_invocations, audit_events (batch-scoped only).
"""
import argparse
import asyncio
import os
import sys
from datetime import datetime, timezone

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "backend"))

SCOPED = ["match_decisions", "exception_cases", "raw_files",
          "model_invocations", "audit_events"]
CUTOFF_FIELD = {"batches": "created_at", "match_decisions": None,
                "exception_cases": None, "raw_files": None,
                "model_invocations": None, "audit_events": None}


def _cutoff_iso(days):
    return datetime.now(timezone.utc).__class__ \
        .fromtimestamp(datetime.now(timezone.utc).timestamp() - days * 86400,
                       tz=timezone.utc).isoformat()


async def _find_old_batches(db, days):
    cutoff = _cutoff_iso(days)
    cur = db.batches.find(
        {"created_at": {"$lt": cutoff}, "archive_of": {"$exists": False}},
        {"_id": 0, "id": 1, "name": 1, "created_at": 1, "sandbox": 1})
    return [b async for b in cur]


async def archive(db, batch_ids, dry):
    moved = {}
    for bid in batch_ids:
        counts = {}
        for coll in SCOPED:
            n = await db[coll].count_documents({"batch_id": bid})
            counts[coll] = n
        counts["batches"] = 1
        if dry:
            print(f"  would archive {bid[:8]} ({counts})")
            continue
        for coll in SCOPED:
            docs = await db[coll].find({"batch_id": bid}).to_list(100000)
            if docs:
                await db[f"archive_{coll}"].insert_many(docs)
                await db[coll].delete_many({"batch_id": bid})
                counts[coll] = len(docs)
        doc = await db.batches.find_one({"id": bid})
        if doc:
            doc["archive_of"] = None
            doc["archived_at"] = datetime.now(timezone.utc).isoformat()
            await db["archive_batches"].insert_one(doc)
            await db.batches.delete_one({"id": bid})
        moved[bid] = counts
        print(f"  archived {bid[:8]} ({counts})")
    return moved


async def restore(db, batch_id):
    doc = await db["archive_batches"].find_one({"id": batch_id})
    if not doc:
        print(f"no archived batch {batch_id}")
        return
    doc.pop("_id", None)
    doc.pop("archived_at", None)
    await db.batches.insert_one(doc)
    total = 1
    for coll in SCOPED:
        docs = await db[f"archive_{coll}"].find({"batch_id": batch_id}).to_list(100000)
        if docs:
            await db[coll].insert_many(docs)
            await db[f"archive_{coll}"].delete_many({"batch_id": batch_id})
            total += len(docs)
    await db["archive_batches"].delete_one({"id": batch_id})
    print(f"restored {batch_id} ({total} docs)")


async def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--days", type=int, default=90)
    ap.add_argument("--batch-id")
    ap.add_argument("--restore", action="store_true")
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    mongo_url = os.environ.get("MONGO_URL", "mongodb://localhost:27017")
    db_name = os.environ.get("DB_NAME", "recon_control_tower")
    from motor.motor_asyncio import AsyncIOMotorClient
    db = AsyncIOMotorClient(mongo_url)[db_name]

    if args.restore:
        await restore(db, args.batch_id)
        return

    olds = await _find_old_batches(db, args.days)
    ids = [o["id"] for o in olds] if not args.batch_id else [args.batch_id]
    print(f"{len(ids)} batch(es) older than {args.days}d"
          f"{' (dry-run)' if args.dry_run else ''}")
    result = await archive(db, ids, args.dry)
    if not args.dry_run and result:
        print(f"done: {len(result)} batches archived")


if __name__ == "__main__":
    asyncio.run(main())
