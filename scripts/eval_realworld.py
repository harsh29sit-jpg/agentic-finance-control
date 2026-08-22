#!/usr/bin/env python3
"""Real-world accuracy & robustness evaluation for the reconciliation engine.

External data: PaySim mobile-money transactions (calibrated on real operator
logs) + narration formats documented for Indian banks (HDFC/ICICI/SBI/Axis,
NEFT/RTGS/IMPS/UPI conventions).

Parts:
  A. External-data conformance  - every real PaySim amount round-trips through
                                  the money parser without loss.
  B. Truth-by-construction      - a 3-ledger batch built FROM the external data
                                  with known injected anomalies; engine scored
                                  on precision/recall/taxonomy vs gates.
  C. Robustness gauntlet        - truncated UTR narrations, hostile merchant
                                  strings, mojibake encodings, malformed rows.
  D. Scale & performance        - 25k settlements sampled from the real
                                  amount distribution.

Usage: .venv/bin/python scripts/eval_realworld.py [--settlements N]
Exit code 0 only if every gate passes.
"""
import csv
import io
import os
import random
import sys
import time
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, os.path.join(ROOT, "backend"))

from adapters import parse_money, parse_date_any, extract_reference, clean_merchant  # noqa: E402
from engine import run_reconciliation, compute_benchmark  # noqa: E402

DATA_DIR = os.path.join(HERE, "data", "realworld")
PAYSIM_URL = ("https://raw.githubusercontent.com/Ansem-chaieb/"
              "PaySim-mobile-money-dataset-for-fraud-detection/main/data/sub_data.csv")
POLICY = {"amount_tolerance_paise": 100, "timing_lag_days": 1}

PASS, FAIL = "\033[32mPASS\033[0m", "\033[31mFAIL\033[0m"
results = []


def check(name, ok, detail=""):
    results.append((name, bool(ok), detail))
    print(f"  [{PASS if ok else FAIL}] {name}" + (f"  -- {detail}" if detail else ""))


# ---------------------------------------------------------------- data loading
def load_paysim():
    path = os.path.join(DATA_DIR, "paysim_sub.csv")
    if not os.path.exists(path):
        os.makedirs(DATA_DIR, exist_ok=True)
        print(f"downloading PaySim sample -> {path}")
        urllib.request.urlretrieve(PAYSIM_URL, path)
    rows = []
    with open(path, newline="", encoding="utf-8", errors="replace") as f:
        for r in csv.DictReader(f):
            try:
                amt = float(r["amount"])
            except (KeyError, ValueError):
                continue
            if r.get("type") in ("PAYMENT", "CASH_OUT", "TRANSFER", "DEBIT") and amt > 0:
                rows.append({"amount": amt, "type": r["type"],
                             "orig": r.get("nameOrig", ""), "dest": r.get("nameDest", ""),
                             "fraud": r.get("isFraud") == "1"})
    return rows


# ---------------------------------------------------------------- builders
BANK_STYLES = [
    lambda utr, m: f"NEFT CR:{utr}/{m}/SETTLEMENT RAZORPAY",
    lambda utr, m: f"NEFT-{utr}-{m}-SETTLEMENT",
    lambda utr, m: f"TRANSFER FROM RAZORPAY SOFTWARE PVT LTD UTR {utr}",
    lambda utr, m: f"RTGS CR {utr} {m} SETTLEMENT",
]
BANK_CODES = ["HDFC", "ICIC", "SBIN", "UTIB"]


def make_utr(rng, seq):
    return f"{rng.choice(BANK_CODES)}26{1 + seq % 365:03d}{seq % 10_000_000:07d}"


def _date(day):
    return f"2026-06-{day:02d}"


def build_ledgers(paysim, n_settlements, seed=2026):
    """Build Source A/B/C rows + ground truth from REAL PaySim amounts."""
    rng = random.Random(seed)
    rows, truth = [], []
    base = 900_000

    def split_payments(gross, k):
        cuts = sorted(rng.sample(range(1, max(2, gross)), k - 1)) if k > 1 else []
        parts, prev = [], 0
        for c in cuts + [gross]:
            parts.append(c - prev)
            prev = c
        return [p for p in parts if p > 0]

    for i in range(n_settlements):
        src = paysim[i % len(paysim)]
        sid = f"SETL_{base + i}"
        merchant = f"M{src['dest'][1:6]}" if src["dest"].startswith("C") else src["dest"]
        gross = int(src["amount"] * 100)
        style = BANK_STYLES[i % len(BANK_STYLES)]
        day = (i % 27) + 1

        scenario = "clean"
        if i % 29 == 7:
            scenario = "tolerance"
        elif i % 37 == 11:
            scenario = "mismatch"
        elif i % 41 == 13:
            scenario = "missing_in_bank"
        elif i % 43 == 17:
            scenario = "duplicate"
        elif i % 31 == 19:
            scenario = "timing_lag"
        elif i % 53 == 23:
            scenario = "missing_in_ledger"

        # --- Source A: payment captures summing to gross
        for j, part in enumerate(split_payments(gross, rng.randint(1, 4))):
            rows.append({"source": "A", "external_id": f"{sid.lower()}_pay{j}",
                         "settlement_id": sid, "utr": "", "amount": str(part),
                         "merchant_id": merchant, "rail": "UPI",
                         "narration": f"payment capture {merchant} order {base+i}{j}",
                         "txn_date": _date(day)})

        if scenario == "missing_in_ledger":
            truth.append({"key": sid, "expected": "exception",
                          "taxonomy": "MISSING_IN_LEDGER", "scenario": scenario})
            continue

        net = gross - int(gross * 0.02)          # 2% TDR
        utr = make_utr(rng, base + i)

        rows.append({"source": "B", "external_id": f"{sid.lower()}_stl",
                     "settlement_id": sid, "utr": utr, "amount": str(net),
                     "merchant_id": merchant, "rail": ["NEFT", "RTGS", "IMPS"][i % 3],
                     "narration": f"settlement {sid} net of TDR",
                     "txn_date": _date(day)})
        truth.append({"key": sid,
                      "expected": "match" if scenario in ("clean", "tolerance", "timing_lag")
                      else "exception",
                      "taxonomy": {"tolerance": None}.get(scenario),
                      "scenario": scenario})
        truth[-1]["taxonomy"] = {"mismatch": "AMOUNT_MISMATCH",
                                 "missing_in_bank": "MISSING_IN_BANK",
                                 "duplicate": "DUPLICATE"}.get(scenario)

        if scenario == "missing_in_bank":
            continue

        if scenario == "mismatch":
            c_amount = net - rng.randint(101, 50_000)
        elif scenario == "tolerance":
            c_amount = net - rng.randint(1, 80)
        else:
            c_amount = net

        bank_day = min(day + 3, 28) if scenario == "timing_lag" else day
        narration = style(utr, merchant)
        credit = {"source": "C", "external_id": f"c_{base+i}", "settlement_id": "",
                  "utr": utr, "amount": str(c_amount), "merchant_id": "",
                  "rail": "", "narration": narration, "txn_date": _date(bank_day)}
        rows.append(credit)
        if scenario == "duplicate":
            dup = dict(credit)
            dup["external_id"] = f"c_{base+i}_dup"
            dup["narration"] = narration + " REPEAT"
            rows.append(dup)

    # --- standalone unidentified credits (keyed by UTR)
    for k in range(max(5, n_settlements // 300)):
        utr = make_utr(rng, 7_700_000 + k)
        rows.append({"source": "C", "external_id": f"c_unid_{k}", "settlement_id": "",
                     "utr": utr, "amount": str(rng.randint(40_000, 700_000)),
                     "merchant_id": "", "rail": "",
                     "narration": f"{rng.choice(['NEFT', 'IMPS'])} CR {utr} UNMAPPED CREDIT REF{k}",
                     "txn_date": _date(15)})
        truth.append({"key": utr, "expected": "exception",
                      "taxonomy": "UNIDENTIFIED_CREDIT", "scenario": "unidentified"})

    return rows, truth


# ---------------------------------------------------------------- Part A
def part_a(paysim):
    print("\nA. EXTERNAL DATA CONFORMANCE (PaySim amounts -> paise)")
    bad, total = 0, 0
    for row in paysim[:1000]:
        total += 1
        paise = parse_money(row["amount"])
        if paise != round(row["amount"] * 100):
            bad += 1
    check(f"PaySim amount fidelity ({total} values)", bad == 0,
          f"{bad} lossy conversions")

    fmt_cases = [
        ("-337,49", None), ("+5257,28", 525728), ("337,49 EUR", 33749),
        ("₹47,83,291", 478329100), ("(123.45)", None), ("−59.99", None),
        ("£1,250.99", 125099), ("100 Dr", 10000), ("abc", None), (-10, None),
    ]
    ok = True
    for raw, want in fmt_cases:
        try:
            got = parse_money(raw)
            good = got == want
        except ValueError:
            good = want is None
        ok &= good
    check("World-format money parsing (EUR comma-decimals, Indian grouping,"
          " accounting negatives, unicode minus)", ok)


# ---------------------------------------------------------------- Part B
def part_b(paysim, n_settlements):
    print(f"\nB. TRUTH-BY-CONSTRUCTION ACCURACY ({n_settlements} settlements from real PaySim amounts)")
    rows, truth = build_ledgers(paysim, n_settlements)
    t0 = time.perf_counter()
    out = run_reconciliation(rows, POLICY)
    elapsed = time.perf_counter() - t0

    score = compute_benchmark(truth, out["match_decisions"], out["exceptions"])

    # taxonomy correctness: injected scenario must yield exactly that taxonomy
    exc_by_key = {}
    for e in out["exceptions"]:
        key = (e.get("settlement_id") or e.get("utr") or "").upper()
        exc_by_key.setdefault(key, e)
    tax_ok, tax_total = 0, 0
    for t in truth:
        if t["expected"] != "exception":
            continue
        tax_total += 1
        e = exc_by_key.get(t["key"].upper())
        if e and e["taxonomy"] == t["taxonomy"]:
            tax_ok += 1

    m = out["metrics"]
    print(f"     rows={len(rows)}  settlements={m['total_settlements']}  "
          f"pass1={m['pass1_matches']} pass2={m['pass2_matches']} "
          f"exceptions={len(out['exceptions'])}  invalid={out['metrics']['invalid_rows']}")
    print(f"     precision={score['auto_match_precision']}%  recall={score['match_recall']}%  "
          f"exc_recall={score['exception_recall']}%  F1={score['f1_score']}  "
          f"FMR={score['false_match_rate']}%  taxonomy={tax_ok}/{tax_total}")

    check("auto-match precision >= 99%", score["auto_match_precision"] >= 99.0,
          f"{score['auto_match_precision']}%")
    check("exception recall == 100%", score["exception_recall"] == 100.0,
          f"missed: {score['missed_matches'][:3]}")
    check("false-match rate < 0.5%", score["false_match_rate"] < 0.5,
          f"{score['false_match_rate']}% (fp={score['false_positive']})")
    check("zero false matches on true exceptions", score["false_positive"] == 0,
          f"dangerous auto-posts: {score['false_matches'][:3]}")
    check("exception taxonomy accuracy == 100%", tax_ok == tax_total,
          f"{tax_ok}/{tax_total}")
    check("no silent drops (every record explicit)",
          m["invalid_rows"] == 0 and
          m["auto_matched"] + len(out["exceptions"]) +
          sum(1 for d in out["match_decisions"] if d["status"] == "pending_review")
          >= m["total_settlements"])
    check("throughput within target (>20k rows/s)", len(rows) / elapsed > 20_000,
          f"{len(rows) / elapsed:,.0f} rows/s")
    return out, score


# ---------------------------------------------------------------- Part C
def part_c():
    print("\nC. ROBUSTNESS GAUNTLET")

    # C1: truncated narration cutting the UTR mid-string (real CSV exports do this)
    utr = "HDFC2616712345678"
    truncated = f"NEFT CR:{utr[:9]}..."                 # cut mid-UTR
    kind, ref = extract_reference(truncated)
    rows = [
        {"source": "B", "external_id": "b1", "settlement_id": "S_TRUNC", "utr": utr,
         "amount": "50000", "merchant_id": "M", "rail": "NEFT",
         "narration": "stl", "txn_date": "2026-06-01"},
        {"source": "C", "external_id": "c1", "settlement_id": "", "utr": ref or "",
         "amount": "50000", "merchant_id": "", "rail": "NEFT",
         "narration": truncated, "txn_date": "2026-06-01"},
    ]
    out = run_reconciliation(rows, POLICY)
    no_false_match = not out["match_decisions"]
    both_flagged = {(e["taxonomy"]) for e in out["exceptions"]} == \
                   {"MISSING_IN_BANK", "UNIDENTIFIED_CREDIT"}
    check("truncated UTR never falsely matches", no_false_match and both_flagged,
          f"ref extracted: {ref!r}; exceptions: {[e['taxonomy'] for e in out['exceptions']]}")

    # C2: hostile merchant strings (real-world names with separators)
    hostile = ["M/S A & B TRADERS", "S.K.U.D (INDIA) PVT LTD, MUMBAI",
               "O'BRIEN & SONS/EXPORTS", "شركة الخليج للتجارة", "日本商事株式会社"]
    ok = True
    for h in hostile:
        rows_h = [{"source": "B", "external_id": "b", "settlement_id": "S_H", "utr": "UTRX123456",
                   "amount": "1000", "merchant_id": "", "rail": "NEFT",
                   "narration": f"NEFT CR:UTRX123456 {h}", "txn_date": "2026-06-01"},
                  {"source": "C", "external_id": "c", "settlement_id": "", "utr": "UTRX123456",
                   "amount": "1000", "merchant_id": "", "rail": "NEFT",
                   "narration": f"NEFT CR:UTRX123456 {h}", "txn_date": "2026-06-01"}]
        o = run_reconciliation(rows_h, POLICY)
        ok &= (len(o["match_decisions"]) == 1 and
               o["match_decisions"][0]["status"] == "matched")
    check("hostile merchant strings (slashes/&/apostrophe/CJK/Arabic) match cleanly", ok)

    # C3: mojibake / latin-1 bytes in narrations (real bank export artifact)
    raw = b"NEFT CR:HDFC2616712345678 Naam initi\xef\xbf\xbdrende partij SETTLEMENT"
    text = raw.decode("utf-8", errors="replace")
    kind, ref = extract_reference(text)
    check("mojibake bytes survive decoding; UTR still extracted",
          ref == "HDFC2616712345678")

    # C4: malformed rows are isolated, counted, never crash the engine
    bad_rows = [
        {"source": "B", "external_id": "ok_b", "settlement_id": "S_OK", "utr": "OKUTR000001",
         "amount": "25000", "merchant_id": "M", "rail": "NEFT", "narration": "x",
         "txn_date": "2026-06-01"},
        {"source": "C", "external_id": "ok_c", "settlement_id": "", "utr": "OKUTR000001",
         "amount": "25000", "merchant_id": "", "rail": "NEFT", "narration": "x",
         "txn_date": "2026-06-01"},
        {"source": "B", "external_id": "bad1", "settlement_id": "S_BAD", "utr": "U2",
         "amount": "abc", "merchant_id": "M", "rail": "NEFT", "narration": "x",
         "txn_date": "2026-06-01"},
        {"source": "C", "external_id": "bad2", "settlement_id": "", "utr": "U3",
         "amount": "-500", "merchant_id": "", "rail": "NEFT", "narration": "x",
         "txn_date": "2026-06-01"},
        {"source": "X", "external_id": "bad3", "settlement_id": "", "utr": "U4",
         "amount": "100", "merchant_id": "", "rail": "NEFT", "narration": "x",
         "txn_date": "2026-06-01"},
        {"source": "A", "external_id": "", "settlement_id": "S_BAD", "utr": "",
         "amount": "100", "merchant_id": "", "rail": "UPI", "narration": "x",
         "txn_date": "2026-06-01"},
        {"source": "C", "external_id": "bad5", "settlement_id": "", "utr": "U5",
         "amount": "not money", "merchant_id": "", "rail": "NEFT", "narration": "x",
         "txn_date": "31/02/2026"},   # impossible date too
    ]
    out = run_reconciliation(bad_rows, POLICY)
    errs = [i["error"] for i in out["invalid"]]
    check("malformed rows isolated with reasons (no crash, no silent drop)",
          out["metrics"]["invalid_rows"] == 5 and len(out["invalid"]) == 5,
          f"errors: {sorted(set(errs))}")
    check("good records still reconciled alongside garbage",
          any(d["settlement_id"] == "S_OK" and d["status"] == "matched"
              for d in out["match_decisions"]))

    # C5: same UTR credited twice by the bank -> duplicate detection
    rows_d = [
        {"source": "B", "external_id": "b", "settlement_id": "S_DUP", "utr": "DUPUTR000001",
         "amount": "80000", "merchant_id": "M", "rail": "NEFT", "narration": "x",
         "txn_date": "2026-06-01"},
        {"source": "C", "external_id": "c1", "settlement_id": "", "utr": "DUPUTR000001",
         "amount": "80000", "merchant_id": "", "rail": "NEFT", "narration": "x",
         "txn_date": "2026-06-01"},
        {"source": "C", "external_id": "c2", "settlement_id": "", "utr": "DUPUTR000001",
         "amount": "80000", "merchant_id": "", "rail": "NEFT", "narration": "x DUP",
         "txn_date": "2026-06-01"},
    ]
    out = run_reconciliation(rows_d, POLICY)
    check("double bank credit flagged DUPLICATE at full exposure",
          len(out["exceptions"]) == 1 and
          out["exceptions"][0]["value_at_risk_paise"] == 80000 and
          not out["match_decisions"])


# ---------------------------------------------------------------- Part D
def part_d(paysim, n_scale):
    print(f"\nD. SCALE & PERFORMANCE ({n_scale:,} settlements)")
    rows, truth = build_ledgers(paysim, n_scale, seed=77)
    t0 = time.perf_counter()
    out = run_reconciliation(rows, POLICY)
    elapsed = time.perf_counter() - t0
    m = out["metrics"]
    rps = len(rows) / elapsed
    print(f"     rows={len(rows):,} matched={m['auto_matched']:,} "
          f"exceptions={len(out['exceptions']):,} latency={elapsed:.2f}s")
    score = compute_benchmark(truth, out["match_decisions"], out["exceptions"])
    check(f"accuracy holds at scale (precision>=99%, recall==100%)",
          score["auto_match_precision"] >= 99.0 and score["exception_recall"] == 100.0,
          f"P={score['auto_match_precision']}% R={score['exception_recall']}%")
    check(f"deterministic throughput > 20k rows/s", rps > 20_000, f"{rps:,.0f} rows/s")
    check("end-to-end batch latency < 15s", elapsed < 15.0, f"{elapsed:.2f}s")


# ---------------------------------------------------------------- main
def main():
    n = 3000
    if "--settlements" in sys.argv:
        n = int(sys.argv[sys.argv.index("--settlements") + 1])

    paysim = load_paysim()
    print(f"loaded {len(paysim)} real PaySim transactions "
          f"(types: {sorted({p['type'] for p in paysim})})")

    part_a(paysim)
    part_b(paysim, n)
    part_c()
    part_d(paysim, max(n, 25_000))

    failed = [r for r in results if not r[1]]
    print("\n" + "=" * 64)
    print(f"REAL-WORLD EVALUATION: {len(results) - len(failed)}/{len(results)} gates passed")
    for name, ok, detail in failed:
        print(f"  FAILED: {name} {detail}")
    sys.exit(1 if failed else 0)


if __name__ == "__main__":
    main()
