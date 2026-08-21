"""Deterministic demo ledger generator + ground-truth labels for benchmark scoring."""
import random

MERCHANTS = ["MERCH_ACME", "MERCH_ZEPTO", "MERCH_NOVA", "MERCH_ORBIT", "MERCH_PIXEL", "MERCH_HELIOS"]
RAILS = ["UPI", "IMPS", "NEFT", "RTGS"]
BANK_PREFIX = {"UPI": "UPI", "IMPS": "IMPS", "NEFT": "NEFT", "RTGS": "RTGS"}

# Which scenarios are truly a reconciled match vs a true exception
MATCH_SCENARIOS = {"clean", "tolerance", "timing_lag"}
EXCEPTION_TAXONOMY = {
    "missing_in_bank": "MISSING_IN_BANK",
    "amount_mismatch": "AMOUNT_MISMATCH",
    "duplicate": "DUPLICATE",
    "missing_in_ledger": "MISSING_IN_LEDGER",
}


def _utr(rail, n):
    return f"{BANK_PREFIX[rail]}{202606:06d}{n:06d}"


def generate_batch(seed=42, num_settlements=42):
    """Return (rows, truth). truth is a list of ground-truth labels for benchmark scoring."""
    rng = random.Random(seed)
    rows = []
    truth = []
    settlement_no = 1000

    for i in range(num_settlements):
        settlement_no += 1
        sid = f"setl_{settlement_no}"
        merchant = rng.choice(MERCHANTS)
        rail = rng.choice(RAILS)
        utr = _utr(rail, settlement_no)
        base_date = f"2026-06-{(i % 27) + 1:02d}"
        bank_date = base_date

        n_pay = rng.randint(1, 5)
        payments = []
        gross = 0
        for j in range(n_pay):
            amt = rng.randint(15000, 900000)
            gross += amt
            payments.append(amt)
        fee = int(gross * 0.02)
        net = gross - fee

        scenario = _scenario_for(i)

        for j, amt in enumerate(payments):
            rows.append({
                "source": "A", "external_id": f"pay_{settlement_no}_{j}",
                "settlement_id": sid, "utr": "", "amount": amt,
                "merchant_id": merchant, "rail": rail,
                "narration": f"payment capture {merchant} order#{settlement_no}{j}",
                "txn_date": base_date,
            })

        # ground-truth label per settlement
        if scenario in MATCH_SCENARIOS:
            truth.append({"key": sid, "expected": "match", "taxonomy": None, "scenario": scenario})
        else:
            truth.append({"key": sid, "expected": "exception",
                          "taxonomy": EXCEPTION_TAXONOMY.get(scenario), "scenario": scenario})

        if scenario == "missing_in_ledger":
            continue

        b_amount = net
        rows.append({
            "source": "B", "external_id": f"stl_{settlement_no}",
            "settlement_id": sid, "utr": utr, "amount": b_amount,
            "merchant_id": merchant, "rail": rail,
            "narration": f"settlement {sid} net of TDR", "txn_date": base_date,
        })

        if scenario == "missing_in_bank":
            continue
        if scenario == "amount_mismatch":
            c_amount = net - rng.randint(500, 5000)
        elif scenario == "tolerance":
            c_amount = net - rng.randint(1, 80)
        else:
            c_amount = net

        if scenario == "timing_lag":
            bank_date = f"2026-06-{min((i % 27) + 4, 28):02d}"

        rows.append({
            "source": "C", "external_id": f"bank_{settlement_no}",
            "settlement_id": "", "utr": utr, "amount": c_amount,
            "merchant_id": merchant, "rail": rail,
            "narration": f"NEFT CR {utr} {merchant} SETTLEMENT RAZORPAY", "txn_date": bank_date,
        })

        if scenario == "duplicate":
            rows.append({
                "source": "C", "external_id": f"bank_{settlement_no}_dup",
                "settlement_id": "", "utr": utr, "amount": c_amount,
                "merchant_id": merchant, "rail": rail,
                "narration": f"NEFT CR {utr} {merchant} SETTLEMENT RAZORPAY DUPLICATE", "txn_date": bank_date,
            })

    # unidentified bank credits (true exceptions keyed by utr)
    for k in range(3):
        rail = rng.choice(RAILS)
        n = 9000 + k
        utr = _utr(rail, n)
        rows.append({
            "source": "C", "external_id": f"bank_unident_{k}",
            "settlement_id": "", "utr": utr, "amount": rng.randint(40000, 700000),
            "merchant_id": "", "rail": rail,
            "narration": f"{rail} CR {utr} UNMAPPED CREDIT REF{n}", "txn_date": "2026-06-15",
        })
        truth.append({"key": utr, "expected": "exception", "taxonomy": "UNIDENTIFIED_CREDIT", "scenario": "unidentified"})

    return rows, truth


def generate_ledgers(seed=42, num_settlements=42):
    rows, _ = generate_batch(seed, num_settlements)
    return rows


def _scenario_for(i):
    m = {
        3: "tolerance", 7: "amount_mismatch", 11: "missing_in_bank", 15: "timing_lag",
        19: "duplicate", 23: "missing_in_ledger", 29: "amount_mismatch", 33: "tolerance", 37: "missing_in_bank",
    }
    return m.get(i, "clean")
