"""Settlement Q&A Agent — read-only, grounded answers with enforced citations.

Groundedness guard: every cited record must resolve to a real settlement_id or
UTR present in the batch context handed to the model. Fabricated citations are
stripped and reported in failed_checks, so an answer can never borrow
credibility from records that do not exist.
"""
from . import runtime
from .contracts import validate_copilot

SYSTEM = (
    "You are the read-only Settlement Q&A Copilot for a Razorpay reconciliation batch. "
    "Answer ONLY from the provided batch context. Cite exact settlement_ids and UTRs "
    "that appear in the context — citations are machine-verified and fabricated ones "
    "are stripped. You cannot mutate any outcome. If the answer is not in context, "
    "say so. Respond ONLY with JSON: {\"answer\": string, "
    "\"cited_records\": [settlement_id or UTR strings], \"failed_checks\": [strings], "
    "\"suggested_next_action\": string}."
)


def _prompt(question, context):
    import json
    return f"BATCH CONTEXT (JSON):\n{json.dumps(context)[:12000]}\n\nQUESTION: {question}"


def _known_identifiers(context):
    known = set()
    for e in context.get("exceptions", []):
        if e.get("settlement_id"):
            known.add(e["settlement_id"].upper())
        if e.get("utr"):
            known.add(e["utr"].upper())
    for m in context.get("matches_sample", []):
        if m.get("settlement_id"):
            known.add(m["settlement_id"].upper())
        if m.get("utr"):
            known.add(m["utr"].upper())
    return known


def _guard_citations(answer: dict, known):
    kept, dropped = [], []
    for c in answer.get("cited_records", []):
        (kept if str(c).upper() in known else dropped).append(str(c))
    answer["cited_records"] = kept
    if dropped:
        answer.setdefault("failed_checks", []).append(
            f"unverifiable citations removed by groundedness guard: {dropped}")
    return answer


async def run(question, context):
    obj, inv = await runtime.invoke_json("copilot", SYSTEM, _prompt(question, context),
                                         validate_copilot)
    inv["verified"] = None  # groundedness recorded separately below
    if obj is None:
        inv["fallback"] = True
        return {"answer": "I could not generate a grounded answer from this batch context.",
                "cited_records": [], "failed_checks": [],
                "suggested_next_action": "Refine the question."}, inv

    data = _guard_citations(obj.model_dump(), _known_identifiers(context))
    inv["verified"] = not any(c.startswith("unverifiable") for c in data["failed_checks"])
    return data, inv
