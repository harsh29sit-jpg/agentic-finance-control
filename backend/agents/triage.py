"""Exception Triage Agent — classifies unmatched cases, drafts next steps.

Contract-validated, one repair round allowed; deterministic fallback preserves
the pipeline when the model is unavailable or non-compliant.
"""
from . import runtime
from .contracts import validate_triage

SYSTEM = (
    "You are the Exception Triage Agent for a Razorpay settlement reconciliation system. "
    "You classify unmatched reconciliation cases and draft resolution suggestions. "
    "Be conservative and cite only evidence present in the case. "
    "You never post or decide a final match. "
    "Respond ONLY with a JSON object: {\"confirmed_taxonomy\": one of "
    "[MISSING_IN_BANK, MISSING_IN_LEDGER, AMOUNT_MISMATCH, DUPLICATE, TIMING_LAG, "
    "NARRATION_AMBIGUOUS, UNIDENTIFIED_CREDIT], \"severity\": \"low\"|\"medium\"|\"high\", "
    "\"suggested_action\": string, \"rationale\": string (<=280 chars)}."
)

FALLBACK_ACTIONS = {
    "MISSING_IN_BANK": ("high", "Raise a bank query for the UTR; confirm rail SLA before escalation."),
    "MISSING_IN_LEDGER": ("medium", "Verify settlement job ran for this settlement_id; re-trigger ingestion."),
    "AMOUNT_MISMATCH": ("high", "Compare TDR/fee schedule; confirm whether delta is fee or short-credit."),
    "DUPLICATE": ("high", "Flag duplicate bank credit; hold reversal and confirm with bank ops."),
    "TIMING_LAG": ("low", "Monitor next business day; auto-clears if credit lands within rail SLA."),
    "NARRATION_AMBIGUOUS": ("medium", "Route to Narration Analysis Agent for candidate linkage."),
    "UNIDENTIFIED_CREDIT": ("medium", "Search narration for merchant/UTR tokens; map to open settlement."),
}


def triage_fallback(case):
    sev, act = FALLBACK_ACTIONS.get(case["taxonomy"], ("medium", "Manual review required."))
    return {"confirmed_taxonomy": case["taxonomy"], "severity": sev,
            "suggested_action": act, "rationale": case["reason"]}


def _rupees(paise):
    return f"₹{(paise or 0) / 100:,.2f}"


def _prompt(case):
    narrations = [c.get("narration") for c in case.get("source_c", [])]
    return (
        f"Case taxonomy (deterministic): {case['taxonomy']}\n"
        f"Reason: {case['reason']}\n"
        f"Settlement: {case.get('settlement_id')}, UTR: {case.get('utr')}, "
        f"Merchant: {case.get('merchant_id')}, Rail: {case.get('rail')}\n"
        f"Value at risk: {_rupees(case.get('value_at_risk_paise'))}\n"
        f"Bank narration(s): {narrations}\n"
        "Classify and suggest the next operational step for an analyst."
    )


async def run(case):
    obj, inv = await runtime.invoke_json("triage", SYSTEM, _prompt(case), validate_triage)
    if obj is not None:
        return obj.model_dump(), inv
    # downgrade to deterministic taxonomy if the model contradicted it without proof
    fb = triage_fallback(case)
    return fb, inv
