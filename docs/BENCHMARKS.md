# Benchmarks

Load-tested through the real HTTP path (`scripts/load_test.py`).
Environment: local Docker-free stack, MongoDB 8 standalone on Apple Silicon,
single uvicorn worker. Numbers scale with hardware; the *ratios* and the
gates are what CI-style reruns should protect.

## Bulk ingestion over HTTP

| Volume | Rows | Wall | Throughput | Status |
|---|---|---|---|---|
| 15k settlements | 65,731 | 1.3 s | **52,517 rows/s** | 200 |
| 40k settlements | 175,105 | 3.6 s | **48,429 rows/s** | 200 |

Deterministic match-rate stays stable (~84.5%) at both volumes — no
accuracy degradation under load.

## Mixed reads under concurrency (dashboard, batches, workbench,
exceptions, agent metrics)

| Concurrency | Reqs | RPS | p50 | p95 | p99 | max | Errors |
|---|---|---|---|---|---|---|---|
| 24 × 40 | 960 | 200 | 114 ms | 214 ms | 251 ms | 310 ms | 0 |
| 50 × 60 | 3000 | 153–194 | ~174 ms | **~700–830 ms** | ~950–1250 ms | ~1.4 s | 0 |

p95 rises with collection size as expected (unbounded exception sorts now
indexed; next lever = read replicas / covering indexes).

## Gates enforced by `scripts/load_test.py`

- ingestion wall ≤ budget (default 120s @ 175k rows)
- zero non-2xx across the mixed-read phase
- read p95 ≤ 900ms at 50 concurrent

## Scaling walls found & fixed during load testing

1. Single-document raw upload storage → BSON 16MB ceiling at ~100k rows.
   Fixed: chunked `raw_files` (20k rows/chunk) + ordered replay.
2. Legacy unique index on `raw_files.batch_id` rejected multi-chunk writes.
   Fixed: startup migration drops it when present.

Rerun anytime:
```bash
.venv/bin/python scripts/load_test.py --base http://localhost:8000 \
  --settlements 40000 --workers 50 --requests-per-worker 60
```

## Frontend rendering

Workbench table now virtualizes (fixed-height windowing hook, zero deps):
DOM rows stay bounded (~30) regardless of the 2000-row fetch cap, so scroll
and filter interactions stay constant-time on large batches.
