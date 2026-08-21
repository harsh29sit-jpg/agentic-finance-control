"""Pydantic request models and shared enums for the reconciliation platform."""
from pydantic import BaseModel, EmailStr, Field
from typing import Optional, List


# ---- Auth ----
class RegisterRequest(BaseModel):
    email: EmailStr
    password: str = Field(min_length=6)
    name: str
    role: str = "analyst"


class LoginRequest(BaseModel):
    email: EmailStr
    password: str


# ---- Review ----
class ReviewAction(BaseModel):
    action: str  # approve | reject | escalate
    note: Optional[str] = ""


class BulkReview(BaseModel):
    batch_id: str
    action: str  # resolve | escalate | reject
    note: Optional[str] = ""
    taxonomy: Optional[str] = None
    ids: Optional[List[str]] = None


class OverrideDecision(BaseModel):
    approve: bool
    note: Optional[str] = ""


# ---- Copilot ----
class CopilotAsk(BaseModel):
    question: str
    batch_id: Optional[str] = None


# ---- Policies ----
class PolicyUpdate(BaseModel):
    amount_tolerance_paise: int = 100
    timing_lag_days: int = 1
    auto_post_confidence: float = 0.95
    note: Optional[str] = ""


# ---- Roles / RBAC ----
ROLES = ["admin", "controller", "compliance", "analyst", "support"]

TAXONOMY = [
    "MISSING_IN_BANK",
    "MISSING_IN_LEDGER",
    "AMOUNT_MISMATCH",
    "DUPLICATE",
    "TIMING_LAG",
    "NARRATION_AMBIGUOUS",
    "UNIDENTIFIED_CREDIT",
]
