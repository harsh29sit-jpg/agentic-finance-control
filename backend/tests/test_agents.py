"""Agent-layer accuracy guarantee tests: contracts, bounded repair, evidence
verification, groundedness guarding, and deterministic fallbacks.
No live LLM required — providers are monkeypatched or absent."""
import asyncio
import json

import pytest

from agents import providers, runtime
from agents.contracts import validate_triage, validate_narration, validate_copilot
from agents.evidence import score_candidates, verify_link
from agents.triage import triage_fallback as _triage_fallback


# ---------------- contracts ----------------
class TestContracts:
    def test_valid_triage_accepted(self):
        raw = json.dumps({"confirmed_taxonomy": "DUPLICATE", "severity": "high",
                          "suggested_action": "Hold reversal", "rationale": "UTR twice"})
        obj, err = validate_triage(raw)
        assert obj is not None and err is None
        assert obj.severity == "high"

    def test_unknown_taxonomy_rejected(self):
        raw = json.dumps({"confirmed_taxonomy": "MADE_UP", "severity": "high",
                          "suggested_action": "x", "rationale": ""})
        obj, err = validate_triage(raw)
        assert obj is None and "unknown taxonomy" in err

    def test_invalid_severity_rejected(self):
        raw = json.dumps({"confirmed_taxonomy": "DUPLICATE", "severity": "urgent",
                          "suggested_action": "x", "rationale": ""})
        obj, err = validate_triage(raw)
        assert obj is None and "severity" in err

    def test_garbage_rejected_cleanly(self):
        obj, err = validate_triage("no json here at all")
        assert obj is None and "JSON" in err

    def test_narration_confidence_bounded(self):
        bad = json.dumps({"candidate_settlement_id": "S1", "evidence_substring": "x",
                          "confidence": 1.5, "explanation": ""})
        obj, err = validate_narration(bad)
        assert obj is None and "confidence" in err

    def test_narration_null_link_allowed(self):
        ok = json.dumps({"candidate_settlement_id": None, "evidence_substring": None,
                         "confidence": 0.0, "explanation": "nothing matches"})
        obj, err = validate_narration(ok)
        assert obj is not None and obj.candidate_settlement_id is None

    def test_copilot_schema_enforced(self):
        missing_answer = json.dumps({"cited_records": [], "failed_checks": []})
        obj, err = validate_copilot(missing_answer)
        assert obj is None and "answer" in err


# ---------------- evidence pre-scoring ----------------
NARRATIONS = ["NEFT CR UTR123456 MERCH_ACME SETTLEMENT RAZORPAY",
              "IMPS CR RANDOMREF UNKNOWN CREDIT"]

CANDIDATES = [
    {"settlement_id": "SETL_1", "utr": "UTR123456", "merchant_id": "MERCH_ACME",
     "amount_paise": 100000},
    {"settlement_id": "SETL_2", "utr": "UTR999999", "merchant_id": "MERCH_ZEPTO",
     "amount_paise": 200000},
]


class TestEvidenceScoring:
    def test_exact_identifier_ranks_first(self):
        ranked = score_candidates(NARRATIONS, CANDIDATES)
        assert ranked[0]["candidate"]["settlement_id"] == "SETL_1"
        assert ranked[0]["score"] >= 3.0  # verbatim UTR hit
        assert ranked[1]["score"] < ranked[0]["score"]

    def test_topk_cap(self):
        many = [{**CANDIDATES[0], "settlement_id": f"S{i}"} for i in range(12)]
        assert len(score_candidates(NARRATIONS, many)) <= 5


class TestEvidenceVerification:
    def setup_method(self):
        self.by_sid = {(c["settlement_id"] or "").upper(): c for c in CANDIDATES}

    def _link(self, sid="SETL_1", ev="NEFT CR UTR123456 MERCH_ACME"):
        from agents.contracts import NarrationLink
        return NarrationLink(candidate_settlement_id=sid, evidence_substring=ev,
                             confidence=0.9, explanation="")

    def test_verified_link_passes(self):
        ok, reason = verify_link(self._link(), NARRATIONS, self.by_sid)
        assert ok is True and "verified" in reason

    def test_fabricated_substring_rejected(self):
        link = self._link(ev="TOTAL FANTASY PAYOUT REF77")
        ok, reason = verify_link(link, NARRATIONS, self.by_sid)
        assert ok is False and "verbatim" in reason

    def test_evidence_not_tied_to_candidate_rejected(self):
        # substring exists in narration but references the OTHER candidate's identifiers
        link = self._link(sid="SETL_2", ev="IMPS CR RANDOMREF")
        ok, reason = verify_link(link, NARRATIONS, self.by_sid)
        assert ok is False and "identifiers" in reason

    def test_unknown_candidate_rejected(self):
        link = self._link(sid="GHOST_SETTLEMENT")
        ok, reason = verify_link(link, NARRATIONS, self.by_sid)
        assert ok is False and "not an open candidate" in reason

    def test_missing_evidence_rejected(self):
        link = self._link(ev=None)
        ok, reason = verify_link(link, NARRATIONS, self.by_sid)
        assert ok is False and "no evidence substring" in reason


# ---------------- runtime ----------------
class TestRuntime:
    @pytest.fixture(autouse=True)
    def clean_provider(self, monkeypatch):
        """Start every runtime test with no provider configured."""
        monkeypatch.setattr(providers, "_SEND", None)

    def _fake_provider(self, monkeypatch, replies):
        calls = {"n": 0}

        async def fake_send(system, prompt):
            i = min(calls["n"], len(replies) - 1)
            calls["n"] += 1
            return replies[i]

        monkeypatch.setattr(providers, "_SEND", fake_send)
        monkeypatch.setattr(providers, "PROVIDER_LABEL", "test-stub")
        return calls

    VALID_TRIAGE = json.dumps({"confirmed_taxonomy": "AMOUNT_MISMATCH", "severity": "high",
                               "suggested_action": "Compare fee schedule", "rationale": "delta"})

    async def _one_call(self):
        return await runtime.invoke_json("triage", "sys", "prompt", validate_triage)

    def test_no_provider_yields_fallback_record(self):
        obj, inv = asyncio.run(self._one_call())
        assert obj is None
        assert inv["fallback"] is True and inv["validated"] is False
        assert inv["model"] == "deterministic"
        assert isinstance(inv["latency_ms"], float)

    def test_valid_output_accepted_without_repair(self, monkeypatch):
        self._fake_provider(monkeypatch, [self.VALID_TRIAGE])
        obj, inv = asyncio.run(self._one_call())
        assert obj is not None and obj.severity == "high"
        assert inv["repaired"] is False and inv["fallback"] is False
        assert inv["validated"] is True and inv["model"] == "test-stub"

    def test_bounded_self_repair_recovers_once(self, monkeypatch):
        self._fake_provider(monkeypatch,
                            ["sorry, prose first {broken", self.VALID_TRIAGE])
        obj, inv = asyncio.run(self._one_call())
        assert obj is not None
        assert inv["repaired"] is True and inv["validation_error"] is None

    def test_persistent_failure_falls_back_after_one_repair(self, monkeypatch):
        self._fake_provider(monkeypatch, ["{bad", "{also bad"])
        obj, inv = asyncio.run(self._one_call())
        assert obj is None and inv["repaired"] is True and inv["fallback"] is True

    def test_contract_violation_triggers_repair_with_reason(self, monkeypatch):
        bad_tax = json.dumps({"confirmed_taxonomy": "NOPE", "severity": "high",
                              "suggested_action": "x", "rationale": ""})
        self._fake_provider(monkeypatch, [bad_tax, self.VALID_TRIAGE])
        obj, inv = asyncio.run(self._one_call())
        assert obj is not None and inv["repaired"] is True


# ---------------- full agent flows ----------------
CASE = {
    "taxonomy": "UNIDENTIFIED_CREDIT",
    "reason": "Bank credit has no matching settlement",
    "settlement_id": "", "utr": "UTR123456", "merchant_id": "",
    "rail": "NEFT", "value_at_risk_paise": 500000,
    "source_c": [{"narration": "NEFT CR UTR123456 MERCH_ACME SETTLEMENT RAZORPAY"}],
}


class TestAgentFlows:
    @pytest.fixture(autouse=True)
    def clean_provider(self, monkeypatch):
        monkeypatch.setattr(providers, "_SEND", None)

    def test_triage_fallback_severity_map(self):
        fb = _triage_fallback(CASE | {"taxonomy": "TIMING_LAG"})
        assert fb["severity"] == "low"
        dup = _triage_fallback(CASE | {"taxonomy": "DUPLICATE"})
        assert dup["severity"] == "high"

    def test_hallucinated_link_zeroed_by_verifier(self, monkeypatch):
        hallucination = json.dumps({
            "candidate_settlement_id": "SETL_1",
            "evidence_substring": "CONFIDENT VIBES ONLY",
            "confidence": 0.99, "explanation": "trust me"})

        async def fake(system, prompt):
            return hallucination

        monkeypatch.setattr(providers, "_SEND", fake)
        from agents.narration import run as narration_run
        link, inv = asyncio.run(narration_run(
            CASE, [{"settlement_id": "SETL_1", "utr": "UTR123456",
                    "merchant_id": "MERCH_ACME", "amount_paise": 500000}]))
        assert link["confidence"] == 0.0
        assert link["candidate_settlement_id"] is None
        assert "Rejected by verifier" in link["explanation"]
        assert inv["verified"] is False

    def test_exact_substring_link_survives_and_is_clamped(self, monkeypatch):
        proposal = json.dumps({
            "candidate_settlement_id": "SETL_1",
            "evidence_substring": "NEFT CR UTR123456 MERCH_ACME",
            "confidence": 0.99, "explanation": "verbatim utr + merchant hit"})

        async def fake(system, prompt):
            return proposal

        monkeypatch.setattr(providers, "_SEND", fake)
        from agents.narration import run as narration_run
        link, inv = asyncio.run(narration_run(
            CASE, [{"settlement_id": "SETL_1", "utr": "UTR123456",
                    "merchant_id": "MERCH_ACME", "amount_paise": 500000}]))
        assert link["candidate_settlement_id"] == "SETL_1"
        assert 0.0 < link["confidence"] <= 0.9   # clamped even though model said 0.99
        assert inv["verified"] is True

    def test_offline_narration_returns_safe_rejection(self):
        from agents.narration import run as narration_run
        link, inv = asyncio.run(narration_run(CASE, []))
        assert link["confidence"] == 0.0
        assert inv["fallback"] is True

    def test_reviewer_fallback_mentions_actionable_path(self):
        from agents.reviewer import run as reviewer_run
        triage = _triage_fallback(CASE)
        text, inv = asyncio.run(reviewer_run(CASE, triage))
        assert "UNIDENTIFIED_CREDIT" in text
        assert inv["agent"] == "reviewer"

    def test_scrub_masks_pan_like_sequences(self):
        dirty = "card 4111 1111 1111 1111 used"
        out = providers.scrub(dirty)
        assert "4111" not in out.replace(out.split()[1], "")  # first group kept by design
        assert "MASKED" in out

    def test_scrub_leaves_utrs_intact(self):
        utr_line = "NEFT CR UTR123456 MERCH_ACME"
        assert providers.scrub(utr_line) == utr_line


class TestCopilotGroundedness:
    CONTEXT = {
        "batch": {"name": "B1"},
        "exceptions": [{"taxonomy": "MISSING_IN_BANK", "settlement_id": "SETL_9",
                        "utr": "UTR9", "merchant": "M", "rail": "UPI",
                        "reason": "r", "value_at_risk_paise": 10, "status": "open"}],
        "matches_sample": [],
    }
    KNOWN = {"SETL_9", "UTR9"}

    def _answer(self, citations):
        return json.dumps({"answer": "a", "cited_records": citations,
                           "failed_checks": [], "suggested_next_action": "n"})

    def _run(self, monkeypatch, citations):
        async def fake(system, prompt):
            return self._answer(citations)

        monkeypatch.setattr(providers, "_SEND", fake)
        from agents.copilot import run as copilot_run
        return asyncio.run(copilot_run("q?", self.CONTEXT))

    def test_fabricated_citation_stripped_and_flagged(self, monkeypatch):
        data, inv = self._run(monkeypatch, ["SETL_9", "GHOST_RECORD"])
        assert "SETL_9" in data["cited_records"]
        assert "GHOST_RECORD" not in data["cited_records"]
        assert any("unverifiable" in c for c in data["failed_checks"])
        assert inv["verified"] is False

    def test_all_real_citations_kept(self, monkeypatch):
        data, inv = self._run(monkeypatch, ["UTR9"])
        assert data["cited_records"] == ["UTR9"]
        assert not any("unverifiable" in c for c in data["failed_checks"])
        assert inv["verified"] is True
