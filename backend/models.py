"""Pydantic request models and shared enums for the reconciliation platform."""
from pydantic import BaseModel, EmailStr, Field
from typing import Optional, List, Literal


# ---- Auth ----
class RegisterRequest(BaseModel):
    email: EmailStr
    password: str = Field(min_length=8, max_length=128)
    name: str = Field(min_length=1, max_length=120)
    role: str = Field(default="analyst", pattern="^(admin|controller|compliance|analyst|support)$")


class LoginRequest(BaseModel):
    email: EmailStr
    password: str = Field(min_length=1, max_length=128)
    totp: Optional[str] = Field(default=None, max_length=10,
                                description="MFA code (TOTP or recovery code)")


# ---- Review ----
ReviewActionType = Literal["approve", "reject", "escalate"]
ExceptionActionType = Literal["resolve", "escalate", "override", "reject"]


class ReviewAction(BaseModel):
    """Workbench match-decision review."""
    action: ReviewActionType
    note: Optional[str] = Field(default="", max_length=2000)


class ExceptionReviewAction(BaseModel):
    """Exception-case review (different verb set than match decisions)."""
    action: ExceptionActionType
    note: Optional[str] = Field(default="", max_length=2000)


class BulkReview(BaseModel):
    batch_id: str = Field(max_length=64)
    action: Literal["resolve", "escalate", "reject"]
    note: Optional[str] = Field(default="", max_length=2000)
    taxonomy: Optional[str] = None
    ids: Optional[List[str]] = Field(default=None, max_length=500)


class OverrideDecision(BaseModel):
    approve: bool
    note: Optional[str] = Field(default="", max_length=2000)


# ---- Copilot ----
class CopilotAsk(BaseModel):
    question: str = Field(min_length=3, max_length=1000)
    batch_id: Optional[str] = Field(default=None, max_length=64)


# ---- Policies ----
class PolicyUpdate(BaseModel):
    amount_tolerance_paise: int = Field(default=100, ge=0, le=1_000_000)
    timing_lag_days: int = Field(default=1, ge=0, le=90)
    auto_post_confidence: float = Field(default=0.95, ge=0.5, le=1.0)
    note: Optional[str] = Field(default="", max_length=500)


# ---- Schedules ----
class ScheduleCreate(BaseModel):
    name: str = Field(min_length=1, max_length=80)
    cron: str = Field(min_length=9, max_length=100,
                      description="Standard 5-field cron: min hour day-of-month month day-of-week (UTC)")
    action: Literal["replay_latest_upload", "sandbox_seed"]
    enabled: bool = True
    note: Optional[str] = Field(default="", max_length=300)


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
