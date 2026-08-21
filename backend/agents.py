"""Bounded AI agents (Claude Sonnet 4.6) for exception triage, narration analysis, reviewer copilot and Q&A.

Guardrails:
  - Agents NEVER mutate final match state. They only produce suggestions/explanations.
  - All outputs are returned with the model + prompt so the caller can persist a model_invocation record.
  - Every agent has a deterministic fallback if the LLM is unavailable, so the pipeline never silently drops.
"""
import os
import json
import uuid
from datetime import datetime, timezone
from emergentintegrations.llm.chat import LlmChat, UserMessage

MODEL_PROVIDER = "anthropic"
MODEL_NAME = "claude-sonnet-4-6"


def _client(system_message, session_id=None):
    return LlmChat(
        api_key=os.environ["EMERGENT_LLM_KEY"],
        session_id=session_id or str(uuid.uuid4()),
        system_message=system_message,
    ).with_model(MODEL_PROVIDER, MODEL_NAME)


async def _ask_json(system, prompt):
    """Send a single non-streaming request expecting a JSON object back."""
    chat = _client(system)
    resp = await chat.send_message(UserMessage(text=prompt))
    text = resp if isinstance(resp, str) else str(resp)
    return _extract_json(text), text


def _extract_json(text):
    try:
        start = text.index("{")
        end = text.rindex("}") + 1
        return json.loads(text[start:end])
    except (ValueError, json.JSONDecodeError):
        return None


def _rupees(paise):
    return f"₹{(paise or 0) / 100:,.2f}"


# ---------------- Exception Triage Agent ----------------
async def triage_exception(case):
    system = (
        "You are the Exception Triage Agent for a Razorpay settlement reconciliation system. "
        "You classify unmatched reconciliation cases and draft resolution suggestions. "
        "You must be conservative and cite only evidence present in the case. "
        "You never post or decide a final match. Respond ONLY with a JSON object with keys: "
        "confirmed_taxonomy (string), severity (low|medium|high), suggested_action (string), "
        "rationale (string, <=280 chars)."
    )
    prompt = (
        f"Case taxonomy (deterministic): {case['taxonomy']}\n"
        f"Reason: {case['reason']}\n"
        f"Settlement: {case.get('settlement_id')}, UTR: {case.get('utr')}, "
        f"Merchant: {case.get('merchant_id')}, Rail: {case.get('rail')}\n"
        f"Value at risk: {_rupees(case.get('value_at_risk_paise'))}\n"
        f"Bank narration(s): {[c.get('narration') for c in case.get('source_c', [])]}\n"
        "Classify and suggest the next operational step for an analyst."
    )
    try:
        data, raw = await _ask_json(system, prompt)
        if data and data.get("suggested_action"):
            return data, _invocation("triage", prompt, raw)
    except Exception as e:  # noqa
        raw = f"error: {e}"
    return _triage_fallback(case), _invocation("triage", prompt, raw if 'raw' in dir() else "fallback")


def _triage_fallback(case):
    actions = {
        "MISSING_IN_BANK": ("high", "Raise a bank query for the UTR; confirm rail SLA before escalation."),
        "MISSING_IN_LEDGER": ("medium", "Verify settlement job ran for this settlement_id; re-trigger ingestion."),
        "AMOUNT_MISMATCH": ("high", "Compare TDR/fee schedule; confirm whether delta is fee or short-credit."),
        "DUPLICATE": ("high", "Flag duplicate bank credit; hold reversal and confirm with bank ops."),
        "TIMING_LAG": ("low", "Monitor next business day; auto-clears if credit lands within rail SLA."),
        "NARRATION_AMBIGUOUS": ("medium", "Route to Narration Analysis Agent for candidate linkage."),
        "UNIDENTIFIED_CREDIT": ("medium", "Search narration for merchant/UTR tokens; map to open settlement."),
    }
    sev, act = actions.get(case["taxonomy"], ("medium", "Manual review required."))
    return {"confirmed_taxonomy": case["taxonomy"], "severity": sev,
            "suggested_action": act, "rationale": case["reason"]}


# ---------------- Narration Analysis Agent ----------------
async def analyze_narration(case, candidates):
    system = (
        "You are the Narration Analysis Agent. You inspect ambiguous bank narration text and propose a "
        "candidate settlement link ONLY when you can point to an exact substring in the narration that "
        "matches a candidate's UTR, settlement_id or merchant. You never auto-post. "
        "Respond ONLY with JSON: {candidate_settlement_id, evidence_substring, confidence (0-1), explanation}."
    )
    narr = " | ".join([c.get("narration", "") for c in case.get("source_c", [])])
    cand_lines = "\n".join(
        [f"- settlement={c['settlement_id']} utr={c['utr']} merchant={c['merchant_id']} amount={_rupees(c['amount_paise'])}"
         for c in candidates]
    )
    prompt = (
        f"Bank narration: \"{narr}\"\n"
        f"Value: {_rupees(case.get('value_at_risk_paise'))}\n"
        f"Candidate open settlements:\n{cand_lines}\n"
        "Return your best evidence-backed candidate or null if none."
    )
    try:
        data, raw = await _ask_json(system, prompt)
        if data:
            return data, _invocation("narration", prompt, raw)
    except Exception as e:  # noqa
        raw = f"error: {e}"
    return ({"candidate_settlement_id": None, "evidence_substring": None, "confidence": 0.0,
             "explanation": "No exact evidence substring found in narration."},
            _invocation("narration", prompt, raw if 'raw' in dir() else "fallback"))


# ---------------- Reviewer Copilot Agent ----------------
async def reviewer_explain(case, triage):
    system = (
        "You are the Reviewer Copilot. Explain concisely why this reconciliation case failed or was routed, "
        "summarizing the deterministic pass outcome and candidate evidence so an analyst can act fast. "
        "Do not decide for them. Respond in 2-3 short sentences, plain text."
    )
    prompt = (
        f"Taxonomy: {case['taxonomy']}\nReason: {case['reason']}\n"
        f"Triage suggestion: {triage.get('suggested_action')}\n"
        f"Value at risk: {_rupees(case.get('value_at_risk_paise'))}"
    )
    try:
        chat = _client(system)
        resp = await chat.send_message(UserMessage(text=prompt))
        text = resp if isinstance(resp, str) else str(resp)
        return text.strip(), _invocation("reviewer", prompt, text)
    except Exception as e:  # noqa
        fb = f"Case failed deterministic passes ({case['taxonomy']}). {case['reason']}. Suggested: {triage.get('suggested_action')}"
        return fb, _invocation("reviewer", prompt, f"fallback: {e}")


# ---------------- Settlement Q&A Copilot ----------------
async def copilot_answer(question, context):
    system = (
        "You are the read-only Settlement Q&A Copilot for a Razorpay reconciliation batch. "
        "Answer ONLY from the provided batch context. Cite exact settlement_ids, UTRs and rule failures. "
        "You cannot mutate any outcome. If the answer is not in context, say so. "
        "Respond ONLY with JSON: {answer (string), cited_records (array of strings), "
        "failed_checks (array of strings), suggested_next_action (string)}."
    )
    prompt = f"BATCH CONTEXT (JSON):\n{json.dumps(context)[:12000]}\n\nQUESTION: {question}"
    try:
        data, raw = await _ask_json(system, prompt)
        if data and data.get("answer"):
            return data, _invocation("copilot", question, raw)
    except Exception as e:  # noqa
        raw = f"error: {e}"
    return ({"answer": "I could not generate a grounded answer from this batch context.",
             "cited_records": [], "failed_checks": [], "suggested_next_action": "Refine the question."},
            _invocation("copilot", question, raw if 'raw' in dir() else "fallback"))


def _invocation(agent, prompt, raw_output):
    return {
        "id": str(uuid.uuid4()),
        "agent": agent,
        "model": f"{MODEL_PROVIDER}/{MODEL_NAME}",
        "prompt_preview": (prompt or "")[:500],
        "output_preview": (raw_output or "")[:1500],
        "created_at": datetime.now(timezone.utc).isoformat(),
    }
