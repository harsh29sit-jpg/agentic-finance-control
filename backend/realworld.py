"""Realistic-data pipeline: public Paysim subset -> Razorpay-shaped A/B/C ledgers.

Why: results produced by a project's own demo generator deserve discounting.
This module reshapes a PUBLIC, independently-produced transaction dataset
(Lopez-Rojas et al. PaySim synthetic mobile-money log; shipped subset:
backend/data/paysim_1000.csv) into the platform's three-ledger settlement
domain with an explicitly declared anomaly profile:

  - Payments (Source A): individual Paysim CASH_OUT/PAYMENT entries become
    captured PG payments grouped under merchant-day settlements.
  - Settlements (Source B): one net settlement per merchant-day at a 2% TDR,
    carrying a NEFT UTR.
  - Bank statement (Source C): one credit per UTR — plus seeded deviations:
    timing lag, fee drift beyond tolerance, duplicate credit, missing credit,
    and unidentified credits (bank rows with no instructed settlement).

Every deviation is deterministic under `seed` and reported in the returned
profile, so benchmark scores are reproducible and auditable.
"""
import csv
import random
from datetime import date, datetime, timedelta
from pathlib import Path

DATA_FILE = Path(__file__).parent / "data" / "paysim_1000.csv"
TDR = 0.02                     # 2% merchant discount rate
TOLERANCE_PAISE = 100          # mirrors default policy


def _paise(rupees):
    return int(round(float(rupees) * 100))


def _utr(no):
    return f"NEFT{no:012d}"


def _day(step):
    return (date(2026, 1, 1) + timedelta(days=int(float(step or 1)) % 28)).isoformat()


def load_paysim(path=None, limit=1000):
    """Read the Paysim subset -> list of {amount, step, orig, dest}."""
    src = Path(path) if path else DATA_FILE
    out = []
    with open(src, newline="", encoding="utf-8") as fh:
        for r in csv.DictReader(fh):
            try:
                amt = float(r["amount"])
                if amt <= 0:
                    continue
            except (KeyError, ValueError, TypeError):
                continue
            out.append({"amount": amt, "step": r.get("step", "1"),
                        "orig": r.get("nameOrig", ""), "dest": r.get("nameDest", "")})
            if len(out) >= limit:
                break
    return out


def shape_ledgers(paysim_rows, seed=7,
                  lag_rate=0.04, drift_rate=0.03, dup_rate=0.02,
                  miss_rate=0.03, unident_rate=0.02):
    """Reshape Paysim entries -> (rows A/B/C, truth labels, profile dict)."""
    rng = random.Random(seed)
    groups = {}
    for p in paysim_rows:
        merch = (p["dest"] or "MUNKNOWN").upper()[:12]
        groups.setdefault((merch, _day(p["step"])), []).append(p)

    rows, truth = [], []
    profile = {"settlements": len(groups), "timing_lag": 0, "amount_drift": 0,
               "duplicates": 0, "missing_in_bank": 0, "unidentified": 0}
    utr_no = 5_000_000_000

    def pay_row(n, i, p, merch, sid, day):
        return {"source": "A", "external_id": f"pay_rw_{n:05d}_{i:03d}",
                "settlement_id": sid.upper(), "utr": "",
                "amount": _paise(p["amount"]), "merchant_id": merch,
                "rail": "UPI", "narration": f"{p['orig']} -> {merch}",
                "txn_date": day}

    def bank_row(utr, amount, merch, day, narration=None, suffix=""):
        return {"source": "C", "external_id": f"BANK-{utr}{suffix}",
                "settlement_id": "", "utr": utr, "amount": int(amount),
                "merchant_id": merch, "rail": "NEFT",
                "narration": narration or f"NEFT CR {utr} RAZORPAY SOFTWARE PVT LTD",
                "txn_date": day}

    n = 0
    for (merch, day) in sorted(groups):
        pays = groups[(merch, day)]
        n += 1
        sid = f"setl_rw_{n:05d}"
        gross = sum(_paise(p["amount"]) for p in pays)
        net = gross - round(gross * TDR)
        utr_no += rng.randint(3, 97)

        roll = rng.random()
        cum = unident_rate

        # --- broken reference: bank credits the value under a UTR that was
        # never instructed -> settlement shows MISSING_IN_BANK while the
        # credit surfaces as UNIDENTIFIED_CREDIT. Recovery links them back.
        if roll < cum:
            utr_no += 1
            utr = _utr(utr_no)
            for i, p in enumerate(pays, start=1):
                rows.append(pay_row(n, i, p, merch, sid, day))
            rows.append({"source": "B", "external_id": sid,
                         "settlement_id": sid.upper(), "utr": utr,
                         "amount": net, "merchant_id": merch, "rail": "NEFT",
                         "narration": f"razorpay settlement {sid}",
                         "txn_date": day})
            orphan_utr = _utr(utr_no + 400_000_000 + n)
            rows.append(bank_row(orphan_utr, net, merch, day,
                                 f"NEFT CR {orphan_utr} UNKNOWN REMITTER"))
            profile["unidentified"] += 1
            truth.append({"key": orphan_utr, "expected": "exception",
                          "taxonomy": "UNIDENTIFIED_CREDIT",
                          "scenario": "realworld_unidentified"})
            continue

        # --- normal A + B legs ----------------------------------------------
        utr_no += 1
        utr = _utr(utr_no)
        for i, p in enumerate(pays, start=1):
            rows.append(pay_row(n, i, p, merch, sid, day))
        rows.append({"source": "B", "external_id": sid,
                     "settlement_id": sid.upper(), "utr": utr, "amount": net,
                     "merchant_id": merch, "rail": "NEFT",
                     "narration": f"razorpay settlement {sid}", "txn_date": day})

        cum += miss_rate
        if roll < cum:                                    # bank never credits
            profile["missing_in_bank"] += 1
            truth.append({"key": sid.upper(), "expected": "exception",
                          "taxonomy": "MISSING_IN_BANK",
                          "scenario": "realworld_missing_in_bank"})
            continue

        cum += lag_rate
        if roll < cum:                                    # late credit, exact sum
            bank_day = (datetime.strptime(day, "%Y-%m-%d").date()
                        + timedelta(days=2)).isoformat()
            rows.append(bank_row(utr, net, merch, bank_day))
            profile["timing_lag"] += 1
            truth.append({"key": sid.upper(), "expected": "match",
                          "taxonomy": None, "scenario": "realworld_timing_lag"})
            continue

        cum += drift_rate
        if roll < cum:                                    # delta beyond tolerance
            drifted = net + TOLERANCE_PAISE * (5 + rng.randint(1, 40)) * rng.choice((1, -1))
            rows.append(bank_row(utr, drifted, merch, day))
            profile["amount_drift"] += 1
            truth.append({"key": sid.upper(), "expected": "exception",
                          "taxonomy": "AMOUNT_MISMATCH",
                          "scenario": "realworld_amount_mismatch"})
            continue

        if rng.random() < dup_rate:                       # double credit
            rows.append(bank_row(utr, net, merch, day))
            rows.append(bank_row(utr, net, merch, day, suffix="-DUP"))
            profile["duplicates"] += 1
            truth.append({"key": sid.upper(), "expected": "exception",
                          "taxonomy": "DUPLICATE", "scenario": "realworld_duplicate"})
            continue

        rows.append(bank_row(utr, net, merch, day))       # clean exact credit
        truth.append({"key": sid.upper(), "expected": "match",
                      "taxonomy": None, "scenario": "realworld_match"})

    return rows, truth, profile


def build_realistic_batch(seed=7, limit=1000, path=None):
    """One-call entrypoint used by server + tests. Returns (rows, truth, profile)."""
    data = load_paysim(path=path, limit=limit)
    if not data:
        raise ValueError("Paysim dataset unavailable/empty")
    return shape_ledgers(data, seed=seed)
