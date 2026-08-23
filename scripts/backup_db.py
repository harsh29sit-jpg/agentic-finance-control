#!/usr/bin/env python3
"""Database backup — mongodump wrapper with retention.

Prefers mongodump (fast, BSON); falls back to a pure-python JSON export of
every collection when mongodump isn't installed (slower, portable).

Usage:
  .venv/bin/python scripts/backup_db.py --out backups --keep 14
Cron example (nightly 02:30):
  30 2 * * * cd /srv/control-tower && .venv/bin/python scripts/backup_db.py --keep 14
"""
import argparse
import json
import os
import shutil
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "backend"))


def _prune(dirpath: Path, keep: int):
    runs = sorted([d for d in dirpath.iterdir() if d.is_dir()], reverse=True)
    for old in runs[keep:]:
        shutil.rmtree(old)
        print(f"pruned {old.name}")


def _mongodump(uri, dbname, out: Path, extra=None):
    cmd = ["mongodump", f"--uri={uri}", f"--db={dbname}", *(
        extra or []), f"--out={out}"]
    subprocess.run(cmd, check=True)
    return True


def _json_fallback(uri, dbname, out: Path):
    from pymongo import MongoClient
    client = MongoClient(uri, serverSelectionTimeoutMS=5000)
    db = client[dbname]
    target = out / dbname
    target.mkdir(parents=True, exist_ok=True)
    for name in db.list_collection_names():
        docs = []
        for doc in db[name].find({}):
            doc["_id"] = str(doc["_id"])
            docs.append(doc)
        (target / f"{name}.json").write_text(json.dumps(docs, default=str))
        print(f"  {name}: {len(docs)} docs")
    return True


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", default="backups")
    ap.add_argument("--keep", type=int, default=14, help="retention count")
    ap.add_argument("--uri", default=os.environ.get("MONGO_URL", "mongodb://localhost:27017"))
    ap.add_argument("--db", default=os.environ.get("DB_NAME", "recon_control_tower"))
    ap.add_argument("--gzip", action="store_true", help="mongodump --gzip")
    ap.add_argument("--s3", default=os.environ.get("BACKUP_S3_BUCKET", ""),
                    help="off-box push target: s3://bucket/prefix (boto3)")
    args = ap.parse_args()

    stamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
    out = Path(args.out) / stamp
    out.mkdir(parents=True, exist_ok=True)

    dump_args = [f"--out={out}"]
    if args.gzip:
        dump_args.append("--gzip")
        # mongodump --gzip writes per-db subdirs; keep --out as-is
    try:
        _mongodump(args.uri, args.db, out, dump_args)
        mode = "mongodump" + ("+gzip" if args.gzip else "")
    except (FileNotFoundError, subprocess.CalledProcessError):
        print("mongodump unavailable — JSON fallback (gzip ignored)")
        _json_fallback(args.uri, args.db, out)
        mode = "json"

    print(f"backup complete [{mode}] -> {out}")

    if args.s3:
        _push_s3(out, args.s3)

    _prune(Path(args.out), args.keep)


def _push_s3(local_dir: Path, s3_target: str):
    """Upload the run to s3://bucket/prefix/stamp/ (server-side copy of tree)."""
    import boto3  # optional dependency

    bucket, _, prefix = s3_target.replace("s3://", "").partition("/")
    s3 = boto3.client("s3")
    uploaded = 0
    for p in sorted(local_dir.rglob("*")):
        if p.is_file():
            key = f"{prefix.rstrip('/')}/{local_dir.name}/{p.relative_to(local_dir)}"
            s3.upload_file(str(p), bucket, key)
            uploaded += 1
    print(f"off-box push: {uploaded} objects -> s3://{bucket}/{prefix}/{local_dir.name}")


if __name__ == "__main__":
    main()
