"""Typed output contracts for every agent.

The LLM proposes; these models dispose. Any reply that fails validation is
repaired once, and if it still fails the agent's deterministic fallback wins.
Nothing unvalidated ever reaches a decision surface.
"""
import json
from typing import Optional, List, Literal
from pydantic import BaseModel, Field, field_validator

TAXONOMY_VALUES = [
    "MISSING_IN_BANK", "MISSING_IN_LEDGER", "AMOUNT_MISMATCH", "DUPLICATE",
    "TIMING_LAG", "NARRATION_AMBIGUOUS", "UNIDENTIFIED_CREDIT",
]


class TriageOutput(BaseModel):
    """Exception Triage Agent contract."""
    confirmed_taxonomy: str = Field()
    severity: Literal["low", "medium", "high"]
    suggested_action: str = Field(min_length=3)
    rationale: str = Field(default="", max_length=500)

    @field_validator("confirmed_taxonomy")
    @classmethod
    def taxonomy_must_be_known(cls, v):
        v = (v or "").strip().upper()
        if v not in TAXONOMY_VALUES:
            raise ValueError(f"unknown taxonomy {v!r}")
        return v


class NarrationLink(BaseModel):
    """Narration Analysis Agent contract — a *proposed* link, never an applied one."""
    candidate_settlement_id: Optional[str] = None
    evidence_substring: Optional[str] = None
    confidence: float = Field(ge=0.0, le=1.0)
    explanation: str = ""

    @field_validator("candidate_settlement_id")
    @classmethod
    def strip_sid(cls, v):
        return (v or "").strip() or None

    @field_validator("evidence_substring")
    @classmethod
    def strip_evidence(cls, v):
        return (v or "").strip() or None


class CopilotAnswer(BaseModel):
    """Settlement Q&A Agent contract."""
    answer: str = Field(min_length=1)
    cited_records: List[str] = Field(default_factory=list, max_length=50)
    failed_checks: List[str] = Field(default_factory=list, max_length=50)
    suggested_next_action: str = ""


# ---- parse+validate helpers used by the runtime (return (obj|None, error|None)) ----
def _parse(model_cls, raw):
    if not raw:
        return None, "empty output"
    try:
        start, end = raw.index("{"), raw.rindex("}") + 1
        data = json.loads(raw[start:end])
    except (ValueError, json.JSONDecodeError) as e:
        return None, f"no valid JSON object ({e.__class__.__name__})"
    try:
        return model_cls.model_validate(data), None
    except Exception as e:  # pydantic ValidationError
        first = getattr(e, "errors", lambda: [])()[0]
        loc = ".".join(str(p) for p in first.get("loc", [])) or "payload"
        return None, f"{loc}: {first.get('msg', 'invalid')}"


def validate_triage(raw):
    return _parse(TriageOutput, raw)


def validate_narration(raw):
    return _parse(NarrationLink, raw)


def validate_copilot(raw):
    return _parse(CopilotAnswer, raw)
