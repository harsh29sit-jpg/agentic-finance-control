#!/usr/bin/env python3
"""HTTP load test at target volume — exercises the REAL deployment path.

Phases:
  A. Bulk ingestion: generate a synthetic 3-ledger CSV (~N settlements),
     POST through the live API, gate on wall time and rows/sec.
  B. Mixed read concurrency: workers hammer dashboard/reconciliation/
     exceptions/metrics simultaneously; gate on error rate + p95 latency.

Gates (tunable):
  --ingest-max-s (default 90)   wall clock for the bulk upload
  --read-p95-ms (default 900)   mixed-read p95 at --workers
  zero non-2xx responses

Usage:
  .venv/bin/python scripts/load_test.py --base http://localhost:8000 \
      --settlements 15000 --workers 24 --requests-per-worker 40
Stdlib + requests only.
"""
import argparse
import csv
import io
import os
import random
import statistics
import sys
import time
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor, as_completed

import requests

BANKS = ["HDFC", "ICIC", "SBIN", "UTIB", "KKBK"]
RAILS = ["NEFT", "RTGS", "IMPS", "UPI"]
MERCH = [f"MERCH_{c}{i:03d}" for i, c in enumerate("ABCDEFGH", start=10)]


def make_ledger(n_settlements, seed=7):
    rng = random.Random(seed)
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(["source", "external_id", "settlement_id", "utr", "amount",
                "merchant_id", "rail", "narration", "txn_date"])
    rows = 0
    base = 5_000_000
    for i in range(n_settlements):
        sid = f"SETL_{base + i}"
        utr = f"{rng.choice(BANKS)}26{rng.randint(1, 365):03d}{rng.randrange(10_000_000):07d}"
        merch = rng.choice(MERCH)
        rail = rng.choice(RAILS)
        day = f"2026-06-{(i % 27) + 1:02d}"
        gross = rng.randrange(15_000, 900_000)          # paise
        n_pay = rng.randint(1, 4)
        cuts = sorted(rng.sample(range(gross // 4, gross - gross // 4 or 2), n_pay - 1)) if n_pay > 1 else []
        parts, prev = [], 0
        for c in cuts + [gross]:
            parts.append(c - prev)
            prev = c
        net = gross - int(gross * 0.02)

        scenario = rng.random()
        for j, part in enumerate(parts):
            w.writerow(["A", f"{sid.lower()}_p{j}", sid, "", str(part), merch,
                        rail, f"payment capture {merch}", day])
            rows += 1
        if scenario > 0.04:                              # 4% missing-in-ledger
            w.writerow(["B", f"{sid.lower()}_b", sid, utr, str(net), merch,
                        "NEFT" if rail != "UPI" else "IMPS",
                        f"settlement {sid}", day])
            rows += 1
        if scenario <= 0.96 and scenario > 0.06:         # bank credit variants
            delta = rng.randint(101, 4000) if scenario < 0.10 else 0
            lag = 3 if 0.10 <= scenario < 0.13 else 0
            w.writerow(["C", f"c_{base+i}", "", utr, str(net - delta),
                        "", "", f"NEFT CR {utr} RAZORPAY SETTLEMENT",
                        f"2026-06-{min((i % 27) + 1 + lag, 28):02d}"])
            rows += 1
            if 0.13 <= scenario < 0.15:                  # duplicate credit
                w.writerow(["C", f"c_{base+i}_dup", "", utr, str(net), "",
                            "", f"NEFT CR {utr} REPEAT", day])
                rows += 1
    return buf.getvalue(), rows


def percentile(sorted_vals, p):
    if not sorted_vals:
        return 0
    k = min(len(sorted_vals) - 1, int(round(p / 100 * (len(sorted_vals) - 1))))
    return sorted_vals[k]


def phase_ingest(base, headers, csv_text, total_rows, max_s):
    print(f"\n[A] bulk ingestion: {total_rows:,} rows over HTTP ...")
    t0 = time.perf_counter()
    files = {"name": (None, "Load Test Batch"),
             "file": ("load.csv", csv_text.encode(), "text/csv")}
    r = requests.post(f"{base}/api/ingestion/upload", headers=headers,
                      files=files, timeout=max(max_s + 120, 300))
    dt = time.perf_counter() - t0
    ok = r.status_code == 200
    counts = r.json().get("counts", {}) if ok else {}
    det = (r.json().get("metrics") or {}).get("deterministic_match_rate") if ok else None
    rps = total_rows / dt if dt else 0
    print(f"    status={r.status_code} wall={dt:.1f}s rows/s={rps:,.0f} "
          f"A·B·C={counts.get('A',0)}·{counts.get('B',0)}·{counts.get('C',0)} det_rate={det}%")
    gate = ok and dt <= max_s
    return {"ok": gate, "wall": dt, "rps": rps}


READ_ENDPOINTS = ["/api/dashboard/metrics", "/api/batches",
                  "/api/reconciliation?limit=200", "/api/exceptions?limit=200",
                  "/api/agents/metrics", "/api/health"]


def phase_reads(base, headers, workers, per_worker):
    print(f"\n[B] mixed reads: {workers} workers x {per_worker} reqs")
    latencies = []
    errors = defaultdict(int)
    lock = __import__("threading").Lock()

    def worker(wid):
        local = []
        s = requests.Session()
        for i in range(per_worker):
            url = base + READ_ENDPOINTS[(wid * per_worker + i) % len(READ_ENDPOINTS)]
            t0 = time.perf_counter()
            try:
                r = s.get(url, headers=headers, timeout=30)
                dtms = (time.perf_counter() - t0) * 1000
                local.append(dtms)
                if r.status_code >= 400:
                    with lock:
                        errors[f"{r.status_code} {url.split('?')[0]}"] += 1
            except Exception as e:  # noqa: BLE001
                with lock:
                    errors[f"{e.__class__.__name__}"] += 1
        with lock:
            latencies.extend(local)

    t0 = time.perf_counter()
    with ThreadPoolExecutor(max_workers=workers) as ex:
        list(ex.map(worker, range(workers)))
    wall = time.perf_counter() - t0
    latencies.sort()
    n = len(latencies)
    summary = {
        "reqs": n, "wall": wall,
        "rps": n / wall if wall else 0,
        "p50": percentile(latencies, 50), "p95": percentile(latencies, 95),
        "p99": percentile(latencies, 99), "max": latencies[-1] if latencies else 0,
        "errors": dict(errors),
    }
    print(f"    reqs={n} wall={wall:.1f}s rps={summary['rps']:,.0f}")
    print(f"    latency ms  p50={summary['p50']:.0f}  p95={summary['p95']:.0f}  "
          f"p99={summary['p99']:.0f}  max={summary['max']:.0f}")
    if errors:
        print(f"    ERRORS: {dict(errors)}")
    return summary


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--base", default=os.environ.get("BASE_URL", "http://localhost:8000"))
    ap.add_argument("--email", default="analyst@recon.io")
    ap.add_argument("--password", default="analyst123")
    ap.add_argument("--settlements", type=int, default=15000)
    ap.add_argument("--workers", type=int, default=24)
    ap.add_argument("--requests-per-worker", type=int, default=40)
    ap.add_argument("--ingest-max-s", type=float, default=90)
    ap.add_argument("--read-p95-ms", type=float, default=900)
    args = ap.parse_args()

    r = requests.post(f"{args.base}/api/auth/login",
                      json={"email": args.email, "password": args.password}, timeout=30)
    assert r.status_code == 200, f"login failed: {r.text[:200]}"
    headers = {"Authorization": f"Bearer {r.json()['token']}"}
    print(f"target {args.base} · authenticated")

    csv_text, total_rows = make_ledger(args.settlements)
    a = phase_ingest(args.base, headers, csv_text, total_rows, args.ingest_max_s)
    b = phase_reads(args.base, headers, args.workers, args.requests_per_worker)

    gates = [
        ("ingestion within budget", a["ok"]),
        ("zero read errors", not b["errors"]),
        (f"read p95 <= {args.read_p95_ms:.0f}ms", b["p95"] <= args.read_p95_ms),
    ]
    print("\n" + "=" * 56)
    failed = False
    for name, ok in gates:
        print(f"  [{'PASS' if ok else 'FAIL'}] {name}")
        failed |= not ok
    print("=" * 56)
    sys.exit(1 if failed else 0)


if __name__ == "__main__":
    main()
