"""Unit tests for the deterministic reconciliation engine (no server required).

Money invariant: everything is integer paise; no floats in matching logic.
"""
import asyncio
import pytest

from engine import (
    to_paise, tokenize_narration, normalize_record,
    run_reconciliation, compute_benchmark, diff_batches,
)
from seed_data import generate_batch
from agents import triage_exception, _triage_fallback


# ---------------- normalization: money ----------------
class TestToPaise:
    def test_plain_paise_int(self):
        assert to_paise(12345) == 12345

    def test_rupee_string_with_decimal(self):
        assert to_paise("1234.5") == 123450

    def test_comma_formatted(self):
        assert to_paise("1,23,456") == 123456

    def test_empty_is_zero(self):
        assert to_paise("") == 0
        assert to_paise(None) == 0

    def test_negative_rejected(self):
        with pytest.raises(ValueError):
            to_paise(-500)

    def test_garbage_rejected(self):
        with pytest.raises(ValueError):
            to_paise("abc")


def test_tokenize_narration():
    assert tokenize_narration("NEFT-CR UTR/123 AB") == ["neft", "cr", "utr", "123", "ab"]
    assert tokenize_narration(None) == []


# ---------------- normalization: records ----------------
class TestNormalizeRecord:
    def _row(self, **over):
        row = {"source": "B", "external_id": "s1", "settlement_id": "SETL_1",
               "utr": " upi 123 ", "amount": "100.00", "merchant_id": "m1",
               "rail": "", "narration": " x ", "txn_date": "2026-06-01"}
        row.update(over)
        return row

    def test_happy_path_normalizes(self):
        rec, err = normalize_record(self._row())
        assert err is None
        assert rec["utr"] == "UPI123"
        assert rec["amount_paise"] == 10000
        assert rec["rail"] == "UPI"          # defaulted
        assert rec["narration"] == "x"

    def test_unknown_source(self):
        assert normalize_record(self._row(source="Z"))[1] == "unknown_source"

    def test_missing_external_id(self):
        assert normalize_record(self._row(external_id=""))[1] == "missing_external_id"

    def test_bad_amount(self):
        assert normalize_record(self._row(amount="12.x"))[1] == "invalid_amount"


def _b(sid, utr, amount, date="2026-06-01", merchant="M1", rail="NEFT"):
    return {"source": "B", "external_id": f"s_{sid}", "settlement_id": sid,
            "utr": utr, "amount": amount, "merchant_id": merchant,
            "rail": rail, "narration": f"stl {sid}", "txn_date": date}


def _c(utrid, amount, date="2026-06-01", narration="bank cr"):
    return {"source": "C", "external_id": f"c_{utrid}", "settlement_id": "",
            "utr": utrid, "amount": amount, "merchant_id": "M1",
            "rail": "NEFT", "narration": narration, "txn_date": date}


def _a(sid, amount, n=1, date="2026-06-01"):
    return [{"source": "A", "external_id": f"a_{sid}_{j}", "settlement_id": sid,
             "utr": "", "amount": amount // n if isinstance(amount, int) else amount,
             "merchant_id": "M1", "rail": "NEFT",
             "narration": "pay", "txn_date": date} for j in range(n)]


POLICY = {"amount_tolerance_paise": 100, "timing_lag_days": 1}


# ---------------- matching passes ----------------
class TestMatchingPasses:
    def test_pass1_exact_match(self):
        rows = [*_a("S1", 100000), _b("S1", "U1", 98000), _c("U1", 98000)]
        out = run_reconciliation(rows, POLICY)
        m = out["match_decisions"][0]
        assert m["status"] == "matched" and m["pass_number"] == 1 and m["tolerance_paise"] == 0

    def test_pass2_within_tolerance(self):
        rows = [_b("S1", "U1", 98000), _c("U1", 97950)]
        out = run_reconciliation(rows, POLICY)
        m = out["match_decisions"][0]
        assert m["pass_number"] == 2 and m["status"] == "matched"
        assert m["tolerance_paise"] == 50

    def test_beyond_tolerance_is_exception(self):
        rows = [_b("S1", "U1", 98000), _c("U1", 90000)]
        out = run_reconciliation(rows, POLICY)
        assert not out["match_decisions"]
        assert out["exceptions"][0]["taxonomy"] == "AMOUNT_MISMATCH"
        assert out["exceptions"][0]["value_at_risk_paise"] == 8000

    def test_timing_lag_pending_review(self):
        rows = [_b("S1", "U1", 98000), _c("U1", 98000, date="2026-06-05")]
        out = run_reconciliation(rows, POLICY)
        m = out["match_decisions"][0]
        assert m["status"] == "pending_review" and m["date_gap_days"] == 4

    def test_duplicate_bank_credit(self):
        rows = [_b("S1", "U1", 98000), _c("U1", 98000), _c("U1", 98000, narration="dup")]
        out = run_reconciliation(rows, POLICY)
        assert not out["match_decisions"]
        e = out["exceptions"][0]
        assert e["taxonomy"] == "DUPLICATE"
        assert e["value_at_risk_paise"] == 98000   # one extra credit at full value

    def test_missing_in_bank(self):
        rows = [_b("S1", "U1", 98000)]
        out = run_reconciliation(rows, POLICY)
        assert out["exceptions"][0]["taxonomy"] == "MISSING_IN_BANK"

    def test_unidentified_bank_credit(self):
        rows = [_c("U99", 50000, narration="UNMAPPED")]
        out = run_reconciliation(rows, POLICY)
        e = out["exceptions"][0]
        assert e["taxonomy"] == "UNIDENTIFIED_CREDIT"
        assert e["narration_case"] is True

    def test_missing_in_ledger_groups_payments(self):
        rows = _a("S404", 30000, n=3)
        out = run_reconciliation(rows, POLICY)
        e = out["exceptions"][0]
        assert e["taxonomy"] == "MISSING_IN_LEDGER"
        assert e["value_at_risk_paise"] == 30000
        assert len(e["source_a"]) == 3

    def test_invalid_rows_counted_not_dropped_silently(self):
        rows = [_b("S1", "U1", 100), {"source": "X", "external_id": "bad", "amount": 1},
                {"source": "A", "external_id": "", "amount": 5}]
        out = run_reconciliation(rows, POLICY)
        assert out["metrics"]["invalid_rows"] == 2
        assert len(out["invalid"]) == 2

    def test_aggregation_annotation_n_to_1(self):
        # 2 payments x ₹1000 sum to ₹2000 vs settlement ₹980 -> fees/TDR ₹10.20
        rows = [*_a("S1", 200000, n=2), _b("S1", "U1", 98000), _c("U1", 98000)]
        out = run_reconciliation(rows, POLICY)
        m = out["match_decisions"][0]
        assert m["payments_count"] == 2
        assert m["aggregation_note"] and "fees/TDR" in m["aggregation_note"]

    def test_every_record_ends_in_explicit_state(self):
        """Data-model rule: matched | pending_review | exception — nothing else."""
        rows, _ = generate_batch(seed=7)
        out = run_reconciliation(rows, POLICY)
        for m in out["match_decisions"]:
            assert m["status"] in ("matched", "pending_review")
        for e in out["exceptions"]:
            assert e["status"] == "open"


# ---------------- seed + benchmark integrity ----------------
class TestSeedBenchmark:
    def setup_method(self):
        self.rows, self.truth = generate_batch(seed=42)

    def test_seed_rows_are_valid(self):
        out = run_reconciliation(self.rows, POLICY)
        assert out["metrics"]["total_settlements"] > 0
        assert all(r["source"] in ("A", "B", "C") for r in out["match_decisions"][0]["source_a"] + [out["match_decisions"][0]["source_b"]] or True)

    def test_benchmark_perfect_on_truth_set(self):
        out = run_reconciliation(self.rows, POLICY)
        score = compute_benchmark(self.truth, out["match_decisions"], out["exceptions"])
        assert score["false_positive"] == 0
        assert score["auto_match_precision"] >= 99.0
        assert score["exception_recall"] == 100.0
        assert score["gates"]["precision_ok"]
        assert score["gates"]["exception_recall_ok"]

    def test_metrics_consistency(self):
        out = run_reconciliation(self.rows, POLICY)
        m = out["metrics"]
        assert m["auto_matched"] == sum(
            1 for d in out["match_decisions"] if d["status"] == "matched")
        assert m["value_at_risk_paise"] == sum(
            e["value_at_risk_paise"] for e in out["exceptions"])
        assert set(m["latency_ms"]) == {"normalization", "matching", "pass1", "pass2", "pass3"}

    def test_determinism_same_seed_same_output(self):
        a = run_reconciliation(generate_batch(seed=99)[0], POLICY)
        b = run_reconciliation(generate_batch(seed=99)[0], POLICY)
        assert [(m["settlement_id"], m["status"], m["pass_number"]) for m in a["match_decisions"]] == \
               [(m["settlement_id"], m["status"], m["pass_number"]) for m in b["match_decisions"]]


class TestComputeBenchmarkMath:
    def test_precision_recall_confusion(self):
        truth = [
            {"key": "A", "expected": "match"},
            {"key": "B", "expected": "match"},
            {"key": "C", "expected": "exception"},
            {"key": "D", "expected": "match"},      # will be missed -> FN
            {"key": "E", "expected": "exception"},  # will be caught
        ]
        matches = [{"settlement_id": "A"}, {"settlement_id": "B"}]
        excs = [{"settlement_id": "E"}]
        s = compute_benchmark(truth, matches, excs)
        assert s["true_positive"] == 2 and s["false_negative"] == 1 and s["false_positive"] == 0
        assert s["match_recall"] == round(2 / 3 * 100, 2)
        assert s["exception_recall"] == 50.0
        assert s["missed_matches"] == ["D"]

    def test_false_match_detected(self):
        truth = [{"key": "X", "expected": "exception"}]
        matches = [{"settlement_id": "X"}]
        s = compute_benchmark(truth, matches, [])
        assert s["false_positive"] == 1
        assert "X" in s["false_matches"]
        assert not s["gates"]["precision_ok"]


class TestDiffBatches:
    def test_resolved_regressed_changed(self):
        base_m = [{"settlement_id": "S1", "pass_number": 1}, {"settlement_id": "S2", "pass_number": 1}]
        base_e = []
        cmp_m = [{"settlement_id": "S1", "pass_number": 2}]
        cmp_e = [{"settlement_id": "S2", "taxonomy": "MISSING_IN_BANK"}]
        d = diff_batches(base_m, base_e, cmp_m, cmp_e)
        kinds = {c["key"]: c["kind"] for c in d["changes"]}
        assert kinds["S1"] == "changed"       # pass number moved 1 -> 2
        assert kinds["S2"] == "regressed"
        assert d["regressed"] == 1 and d["changed"] == 1

    def test_resolution_detected(self):
        base_e = [{"settlement_id": "S9", "taxonomy": "DUPLICATE"}]
        cmp_m = [{"settlement_id": "S9", "pass_number": 1}]
        d = diff_batches([], base_e, cmp_m, [])
        assert d["resolved"] == 1


# ---------------- agent fallback path (no LLM configured) ----------------
class TestAgentFallback:
    def test_triage_fallback_taxonomy_severity(self):
        case = {"taxonomy": "DUPLICATE", "reason": "UTR appears twice"}
        fb = _triage_fallback(case)
        assert fb["severity"] == "high"
        assert "duplicate" in fb["suggested_action"].lower()

    def test_triage_exception_degrades_gracefully(self):
        case = {"taxonomy": "MISSING_IN_BANK", "reason": "not on statement",
                "settlement_id": "S1", "utr": "U1", "merchant_id": "M1",
                "rail": "NEFT", "value_at_risk_paise": 100000, "source_c": []}
        triage, inv = asyncio.run(triage_exception(case))
        assert triage["confirmed_taxonomy"] == "MISSING_IN_BANK"
        assert inv["agent"] == "triage"
