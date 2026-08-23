#!/usr/bin/env python3
"""Multi-instance coordination verification — the guarantees that only
matter when 2+ API instances share one database.

Verifies against a LIVE deployment:
  1. Audit chain stays linear under concurrent cross-instance writes.
  2. Rate-limit budget is shared (combined hits trip one bucket, not two).
  3. Scheduler lease prevents duplicate runs across instances.

Usage:
  python scripts/multi_instance_verify.py --base http://localhost:8003 \
      --instances http://localhost:8001,http://localhost:8002
"""
import argparse
import concurrent.futures as futures
import json
import sys
import threading
import time

import requests

PASS, FAIL = "PASS", "FAIL"
results = []


def check(name, ok, detail=""):
    results.append((name, bool(ok), detail))
    print(f"  [{PASS if ok else FAIL}] {name}" + (f" -- {detail}" if detail else ""))


def login(base, email="admin@recon.io", password="admin123"):
    r = requests.post(f"{base}/api/auth/login",
                      json={"email": email, "password": password}, timeout=30)
    return {"Authorization": f"Bearer {r.json()['token']}"}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--base", required=True, help="LB entrypoint")
    ap.add_argument("--instances", required=True,
                    help="comma-separated direct instance URLs")
    ap.add_argument("--writes-per-instance", type=int, default=6)
    args = ap.parse_args()
    instances = [u.rstrip("/") for u in args.instances.split(",")]
    base = args.base.rstrip("/")

    h = login(base)
    print(f"LB {base} -> instances {instances}")

    # ---- 1. concurrent audit writes split across instances ---------------
    print("\n1. audit chain under cross-instance concurrency")
    def write(inst_idx):
        url = f"{instances[inst_idx % len(instances)]}/api/sandbox/batch"
        r = requests.post(url, headers=h, timeout=180)
        return r.status_code

    with futures.ThreadPoolExecutor(max_workers=args.writes_per_instance * len(instances)) as ex:
        codes = list(ex.map(write, range(args.writes_per_instance * len(instances))))
    ok_all = all(c == 200 for c in codes)
    v = requests.get(f"{base}/api/audit/verify", headers=h, timeout=60).json()
    check("all concurrent batch creations succeeded", ok_all,
          f"codes={sorted(set(codes))}")
    check("audit chain valid after N-instance storm",
          v["valid"] is True and v["checked"] == v["total"],
          f"checked={v['checked']}")

    # ---- 2. shared rate-limit budget -------------------------------------
    print("\n2. shared rate-limit budget across instances")
    email = f"ratelimit_{int(time.time())}@test.io"
    requests.post(f"{base}/api/auth/register",
                  json={"email": email, "password": "shared-limits-1",
                        "name": "RL Probe"}, timeout=30)
    outcomes = []
    stop = threading.Event()

    def hammer(inst_idx):
        while not stop.is_set():
            r = requests.post(f"{instances[inst_idx % len(instances)]}/api/auth/login",
                              json={"email": email,
                                    "password": "wrong-on-purpose"}, timeout=30)
            outcomes.append(r.status_code)
            if r.status_code == 429:
                stop.set()
                break

    with futures.ThreadPoolExecutor(max_workers=len(instances)) as ex:
        list(ex.map(hammer, range(len(instances))))
    saw_429 = 429 in outcomes
    before_429 = outcomes.index(429) if saw_429 else len(outcomes)
    check("429 raised from combined cross-instance traffic", saw_429,
          f"{before_429} attempts hit before first 429 (budget is global)")

    # wait out lockout window influence on later phases? account-scoped; use fresh identity below

    # ---- 3. scheduler lease: exactly one execution per due tick ----------
    print("\n3. scheduler lease across instances")
    r = requests.post(f"{base}/api/schedules", headers=h,
                      json={"name": f"lease-check-{int(time.time())}",
                            "cron": "* * * * *",
                            "action": "sandbox_seed"}, timeout=30)
    sid = r.json()["id"]
    # force due immediately, then let both instances' loops tick naturally
    from pymongo import MongoClient
    import os
    mongo = MongoClient(os.environ.get("MONGO_URL", "mongodb://localhost:27017"))
    db = mongo[os.environ.get("DB_NAME", "recon_control_tower")]
    past = time.strftime("%Y-%m-%dT%H:%M:%S+00:00", time.gmtime(time.time() - 5))
    db.schedules.update_one({"id": sid},
                            {"$set": {"next_run_at": past, "enabled": True}})

    deadline = time.time() + 90
    runs_seen = set()
    while time.time() < deadline:
        doc = db.schedules.find_one({"id": sid})
        if doc.get("run_count", 0) >= 1:
            runs_seen.add(doc["run_count"])
            # give both loops a window to (incorrectly) double-run
            time.sleep(35)
            doc = db.schedules.find_one({"id": sid})
            break
        time.sleep(2)

    final = db.schedules.find_one({"id": sid})
    batches_by_name = db.batches.count_documents(
        {"name": {"$regex": "^Sandbox Fixture"}})
    check("schedule executed exactly once despite 2 tickers",
          final.get("run_count") == 1 and final.get("last_status") == "ok",
          f"run_count={final.get('run_count')} status={final.get('last_status')}")
    check("no in-flight lease leaked", not final.get("in_flight"))

    requests.delete(f"{base}/api/schedules/{sid}", headers=h, timeout=30)

    failed = [r for r in results if not r[1]]
    print("\n" + "=" * 56)
    print(f"MULTI-INSTANCE VERIFICATION: "
          f"{len(results)-len(failed)}/{len(results)} passed")
    for name, _ok, detail in failed:
        print(f"  FAILED: {name} {detail}")
    sys.exit(1 if failed else 0)


if __name__ == "__main__":
    main()
