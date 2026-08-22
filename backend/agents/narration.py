"""Narration Analysis Agent — proposes candidate links with verifiable evidence.

Flow: deterministic pre-scoring grounds the model -> model proposes a link ->
the evidence verifier adjudicates it. Unverified links are returned with
confidence 0 and the rejection reason recorded. The agent can never apply,
post, or mutate anything.
"""
from . import runtime
from .contracts import validate_narration, NarrationLink
from .evidence import score_candidates, verify_link

SYSTEM = (
    "You are the Narration Analysis Agent. You inspect ambiguous bank narration text "
    "and propose a candidate settlement link ONLY when you can point to an exact "
    "substring of the narration that contains the candidate's UTR, settlement_id or "
    "merchant identifier. You never auto-post; a deterministic verifier will check "
    "your evidence and discard unproven claims. "
    "Respond ONLY with JSON: {\"candidate_settlement_id\": string|null, "
    "\"evidence_substring\": exact substring from narration|null, "
    "\"confidence\": 0-1, \"explanation\": string}. "
    "If no candidate has verbatim identifier support in the narration, return nulls."
)


def _rupees(paise):
    return f"₹{(paise or 0) / 100:,.2f}"


def _prompt(case, ranked):
    narr = " | ".join([c.get("narration", "") for c in case.get("source_c", [])])
    lines = "\n".join(
        f"- settlement={r['candidate']['settlement_id']} utr={r['candidate']['utr']} "
        f"merchant={r['candidate']['merchant_id']} amount={_rupees(r['candidate']['amount_paise'])} "
        f"(pre-score {r['score']})"
        for r in ranked)
    return (
        f"Bank narration: \"{narr}\"\n"
        f"Value: {_rupees(case.get('value_at_risk_paise'))}\n"
        f"Candidate open settlements (pre-scored):\n{lines}\n"
        "Propose your best evidence-backed link or nulls if none is supported."
    )


def _candidates_by_sid(candidates):
    return {(c.get("settlement_id") or "").upper(): c for c in candidates}


async def run(case, candidates):
    """Returns (link_dict, invocation). Verified proposals keep their confidence;
    unverifiable ones come back zeroed with the rejection reason."""
    narrations = [c.get("narration", "") for c in case.get("source_c", [])]
    ranked = score_candidates(narrations, candidates)

    obj, inv = await runtime.invoke_json("narration", SYSTEM, _prompt(case, ranked),
                                         validate_narration)
    inv["verified"] = False

    if obj is None:
        inv["fallback"] = True
        return _rejected("model output failed contract validation"), inv

    by_sid = _candidates_by_sid(candidates)
    ok, reason = verify_link(obj, narrations, by_sid)
    inv["verified"] = ok
    data = obj.model_dump()
    if not ok:
        data.update({"candidate_settlement_id": None, "evidence_substring": None,
                     "confidence": 0.0, "explanation": f"Rejected by verifier: {reason}"})
    else:
        # clamp confidence for weak-but-verified evidence
        data["confidence"] = min(data["confidence"], 0.9)
    return data, inv


def _rejected(reason):
    return {"candidate_settlement_id": None, "evidence_substring": None,
            "confidence": 0.0, "explanation": f"No verified link ({reason})."}
