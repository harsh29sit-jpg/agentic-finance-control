"""Reviewer Copilot Agent — analyst-facing explanations, never decisions."""
from . import runtime

SYSTEM = (
    "You are the Reviewer Copilot. Explain concisely why this reconciliation case "
    "failed or was routed, summarizing the deterministic pass outcome and candidate "
    "evidence so an analyst can act fast. Do not decide for them. Respond in 2-3 "
    "short sentences, plain text."
)


def _rupees(paise):
    return f"₹{(paise or 0) / 100:,.2f}"


def _prompt(case, triage):
    return (
        f"Taxonomy: {case['taxonomy']}\nReason: {case['reason']}\n"
        f"Triage suggestion: {triage.get('suggested_action')}\n"
        f"Value at risk: {_rupees(case.get('value_at_risk_paise'))}"
    )


async def run(case, triage):
    text, inv = await runtime.invoke_text("reviewer", SYSTEM, _prompt(case, triage))
    if text:
        return text, inv
    fb = (f"Case failed deterministic passes ({case['taxonomy']}). {case['reason']}. "
          f"Suggested: {triage.get('suggested_action')}")
    return fb, inv
