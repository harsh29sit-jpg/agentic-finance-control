"""Deterministic evidence layer.

Two jobs, both deliberately LLM-free:
  1. Pre-score candidate settlements for a narration case so the model only
     ever sees plausible, token-grounded candidates (smaller hallucination
     surface, smaller prompts).
  2. VERIFY a proposed narration link against the raw evidence. The model may
     propose; only an exact substring hit in real narration text, tied to the
     candidate's UTR / settlement_id / merchant_id, survives verification.
     Unverified links are zeroed out — never silently accepted.
"""
import re

_TOKEN_SPLIT = re.compile(r"[^a-zA-Z0-9]+")
TOP_K_CANDIDATES = 5


def _tokens(text):
    return {t for t in _TOKEN_SPLIT.split((text or "").lower()) if t}


def _identifiers(cand):
    ids = [cand.get("settlement_id") or "", cand.get("utr") or "",
           cand.get("merchant_id") or ""]
    return [i.lower() for i in ids if i]


def score_candidates(narrations, candidates):
    """Rank candidates by exact-identifier and token overlap with the narrations.

    Score composition:
      +3 per identifier appearing verbatim in any narration (strongest signal)
      +1 per shared token between narration and identifier tokens (weak signal)
    Returns top-K as [{"candidate": c, "score": s}, ...] sorted desc.
    """
    joined = " | ".join(narrations or []).lower()
    scored = []
    for cand in candidates:
        score = 0.0
        for ident in _identifiers(cand):
            if ident and ident in joined:
                score += 3.0
            else:
                ident_tokens = _tokens(ident)
                if ident_tokens:
                    score += 0.2 * len(ident_tokens & _tokens(joined))
        scored.append({"candidate": cand, "score": round(score, 2)})
    scored.sort(key=lambda x: -x["score"])
    return scored[:TOP_K_CANDIDATES]


def verify_link(link, narrations, candidates_by_sid):
    """Adjudicate a proposed NarrationLink against raw evidence.

    Returns (verified: bool, reason: str). On failure the caller must treat the
    link as confidence=0 / no candidate.
    """
    if not link or not link.candidate_settlement_id:
        return False, "no candidate proposed"
    cand = candidates_by_sid.get(link.candidate_settlement_id.upper())
    if not cand:
        return False, f"proposed settlement {link.candidate_settlement_id!r} is not an open candidate"
    if not link.evidence_substring:
        return False, "no evidence substring provided"

    ev = link.evidence_substring.strip()
    ev_low = ev.lower()
    joined = " | ".join(narrations or [])
    if ev_low not in joined.lower():
        return False, "evidence substring does not appear verbatim in bank narration"

    idents = [cand.get("settlement_id", ""), cand.get("utr", ""), cand.get("merchant_id", "")]
    if not any(i and i.lower() in ev_low for i in idents):
        return False, "evidence substring does not reference the proposed candidate's identifiers"

    return True, "exact evidence substring verified against narration and candidate identifiers"
