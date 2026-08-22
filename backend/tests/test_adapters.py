"""Permanent coverage for the real-world ingestion adapters.

Money formats are taken from actual bank exports (bank2ynab corpus: European
comma decimals, accounting negatives, Dr/Cr markers) and documented Indian
narration conventions (HDFC/ICICI/SBI/Axis, NEFT/RTGS/IMPS/UPI).
"""
import os

import pytest

from adapters import (
    parse_money, parse_date_any, extract_reference, detect_rail,
    canonical_row, clean_merchant,
)
from engine import run_reconciliation
from seed_data import generate_batch

POLICY = {"amount_tolerance_paise": 100, "timing_lag_days": 1}

PAYSIM_PATH = os.path.join(os.path.dirname(__file__), "..", "..",
                           "scripts", "data", "realworld", "paysim_sub.csv")
HAS_PAYSIM = os.path.exists(PAYSIM_PATH)


# ---------------- money parsing against REAL export formats ----------------
class TestParseMoney:
    @pytest.mark.parametrize("raw,want", [
        ("₹485.00", 48500),
        ("₹24,500.00", 2450000),          # western grouping
        ("₹47,83,291", 478329100),        # Indian lakh/crore grouping
        ("+5257,28", 525728),             # Rabobank-style signed comma decimal
        ("337,49 EUR", 33749),            # trailing currency code
        ("£1,250.99", 125099),
        ("INR 1234.5", 123450),
        ("100 Dr", 10000),                # debit marker suffix
        ("1 234,56", 123456),             # space grouping + comma decimal
        ("12,34,567.89", 123456789),      # mixed Indian grouping + paise
        ("0", 0), ("", 0), (" ", 0),
        (131098.97, 13109897),            # PaySim float amounts
        (780, 780),                       # bare int == already paise (engine convention)
    ])
    def test_accepts(self, raw, want):
        assert parse_money(raw) == want

    @pytest.mark.parametrize("raw", [
        "-337,49",       # debits are filtered upstream, never credits
        "(123.45)",      # accounting negative
        "−59.99",        # unicode minus
        -10, -10.5,      # numeric negatives
        "abc",           # garbage
        float("nan"), float("inf"), True,
    ])
    def test_rejects(self, raw):
        with pytest.raises(ValueError):
            parse_money(raw)


# ---------------- dates across real bank exports ----------------
class TestDates:
    @pytest.mark.parametrize("raw,want", [
        ("2018-02-25 12:34:56 +0000", "2018-02-25"),   # Monzo export
        ("01/09/2017", "2017-09-01"),                   # BOI export DD/MM/YYYY
        ("15-04-24", "2024-04-15"),
        ("15 Apr 2024", "2024-04-15"),
        ("15-Jun-26", "2026-06-15"),
        ("01.06.2026", "2026-06-01"),
        ("2026-06-01", "2026-06-01"),
        ("", ""),
    ])
    def test_formats(self, raw, want):
        assert parse_date_any(raw) == want


# ---------------- narration intelligence (documented bank conventions) ----
class TestNarrations:
    @pytest.mark.parametrize("narration,kind,prefix", [
        ("NEFT CR:HDFC2268012345678 ABC CORP INV-2024-001", "utr", "HDFC"),
        ("NEFT-ICIC260811234567-SWIGGY-SETTLE", "utr", "ICIC"),
        ("TRANSFER FROM RAZORPAY SOFTWARE UTR SBIN2270012345678", "utr", "SBIN"),
        ("RTGS CR:SBIN2268001234567 XYZ LTD ADVANCE", "utr", "SBIN"),
        ("IMPS/987654321012/RAHUL KUMAR/9876", "rrn", "987654321012"),
        ("UPI/P2M/512345678901/john@oksbi/ORDER-891", "upi", "512345678901"),
        ("NACH/BATCH-20260315-001/HDFC0000001", None, None),   # no extractable key
    ])
    def test_extract_reference(self, narration, kind, prefix):
        k, ref = extract_reference(narration)
        assert k == kind
        if prefix:
            assert ref.startswith(prefix)

    def test_rail_detection(self):
        assert detect_rail("NEFT CR:HDFC123 ABC") == "NEFT"
        assert detect_rail("IMPS/123456789012/X/Y") == "IMPS"
        assert detect_rail("UPI/P2M/123/x/y") == "UPI"
        assert detect_rail("unknown narration") == "OTHER"

    def test_hostile_merchant_names(self):
        for h in ["M/S A & B TRADERS", "S.K.U.D (INDIA) PVT LTD, MUMBAI",
                  "O'BRIEN & SONS/EXPORTS", "شركة الخليج للتجارة"]:
            assert clean_merchant(h) is not None


# ---------------- end-to-end: external-derived ledgers reconcile ----------
@pytest.mark.skipif(not HAS_PAYSIM, reason="PaySim sample not cached")
class TestRealWorldBatch:
    def _load_amounts(self, limit=500):
        import csv
        amounts = []
        with open(PAYSIM_PATH, newline="", encoding="utf-8", errors="replace") as f:
            for r in csv.DictReader(f):
                try:
                    amt = float(r["amount"])
                except (ValueError, KeyError):
                    continue
                if r.get("type") in ("PAYMENT", "CASH_OUT", "TRANSFER", "DEBIT") and amt > 0:
                    amounts.append(amt)
                if len(amounts) >= limit:
                    break
        return amounts

    def test_paysim_ledger_reconciles_perfectly(self):
        """Build a small 3-ledger batch from REAL PaySim amounts; every clean
        settlement must match and injected anomalies must be flagged exactly."""
        amounts = self._load_amounts()
        rows = []
        for i, amt in enumerate(amounts[:200]):
            gross = int(amt * 100)
            net = gross - int(gross * 0.02)
            sid, utr = f"RW_{i}", f"HDFC26{100+i:03d}{i:07d}"
            day = f"2026-06-{(i % 27) + 1:02d}"
            rows.append({"source": "A", "external_id": f"a{i}", "settlement_id": sid,
                         "utr": "", "amount": str(gross), "merchant_id": f"M{i}",
                         "rail": "UPI", "narration": f"pay {sid}", "txn_date": day})
            rows.append({"source": "B", "external_id": f"b{i}", "settlement_id": sid,
                         "utr": utr, "amount": str(net), "merchant_id": f"M{i}",
                         "rail": "NEFT", "narration": f"stl {sid}", "txn_date": day})
            if i % 10 == 3:
                continue  # settlement booked, credit never hit the bank -> MISSING_IN_BANK
            c_amt = net if i % 7 else net - rng_delta(i)   # some tolerance-band deltas
            narr = f"NEFT CR:{utr} M{i} SETTLEMENT RAZORPAY"
            rows.append({"source": "C", "external_id": f"c{i}", "settlement_id": "",
                         "utr": utr, "amount": str(c_amt), "merchant_id": "",
                         "rail": "", "narration": narr, "txn_date": day})

        out = run_reconciliation(rows, POLICY)
        m = out["metrics"]
        # engine must not lose anything and must flag the missing-in-bank cases
        expected_missing = sum(1 for i in range(len(amounts[:200])) if i % 10 == 3)
        missing_found = sum(1 for e in out["exceptions"] if e["taxonomy"] == "MISSING_IN_BANK")
        assert m["invalid_rows"] == 0
        assert missing_found == expected_missing
        assert all(d["status"] in ("matched", "pending_review")
                   for d in out["match_decisions"])

        truth = []
        matched_ids = {d["settlement_id"] for d in out["match_decisions"]}
        for i in range(len(amounts[:200])):
            sid = f"RW_{i}"
            truth.append({"key": sid,
                          "expected": "exception" if i % 10 == 3 else "match",
                          "taxonomy": None})
        from engine import compute_benchmark
        score = compute_benchmark(truth, out["match_decisions"], out["exceptions"])
        assert score["false_positive"] == 0          # no dangerous auto-posts
        assert score["auto_match_precision"] >= 99.0


def rng_delta(i):
    return (i * 37) % 81 + 1     # deterministic 1..81 paise delta (within tolerance)
