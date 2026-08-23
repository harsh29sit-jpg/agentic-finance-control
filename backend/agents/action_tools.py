"""Action tools — state-changing capabilities granted to the agent.

Every tool delegates to backend/services.py, so RBAC, maker-checker policy
and tamper-evident audit logging are enforced on exactly the same code path
the UI uses. An agent can never gain a permission its human operator lacks:
each tool declares `allowed_roles` AND the service re-checks.

Tools raise on failure; the orchestrator feeds the error back to the model
so it can correct course or explain.
"""
from pydantic import BaseModel, Field
from typing import Optional, Literal

import services as svc

WRITER_ROLES = ("analyst", "controller", "admin")
CHECKER_ROLES = ("controller", "admin")


class ResolveExceptionArgs(BaseModel):
    case_id: str = Field(description="exception case id OR settlement_id (e.g. SETL_1030)")
    note: str = Field(default="", max_length=500)


class OverrideArgs(ResolveExceptionArgs):
    pass


class DecideOverrideArgs(BaseModel):
    case_id: str
    approve: bool = Field(description="true=approve override, false=reject")
    note: str = Field(default="", max_length=500)


class ReviewMatchArgs(BaseModel):
    decision_id: str = Field(description="match decision id")
    action: Literal["approve", "reject", "escalate"]
    note: str = Field(default="", max_length=500)


class BulkExceptionsArgs(BaseModel):
    batch_id: str
    action: Literal["resolve", "escalate", "reject"]
    taxonomy: Optional[str] = Field(default=None,
                                    description="restrict bulk action to one taxonomy")
    note: str = Field(default="", max_length=500)


class RunSandboxArgs(BaseModel):
    pass


class RerunBatchArgs(BaseModel):
    batch_id: str


class CreateScheduleArgs(BaseModel):
    name: str = Field(max_length=80)
    cron: str = Field(description="5-field cron, UTC. e.g. '0 6 * * *' = daily 06:00")
    action: Literal["replay_latest_upload", "sandbox_seed"]


class CreatePolicyArgs(BaseModel):
    amount_tolerance_paise: int = Field(default=100, ge=0, le=1_000_000)
    timing_lag_days: int = Field(default=1, ge=0, le=90)
    auto_post_confidence: float = Field(default=0.95, ge=0.5, le=1.0)
    note: str = Field(default="", max_length=300)


def _role(user):
    return user.get("role")


def _require(user, roles):
    if _role(user) not in roles:
        raise PermissionError(
            f"requires role {' or '.join(roles)} (you are {_role(user)})")


# ---------------------------------------------------------------- runners
async def _resolve(db, user, a):
    _require(user, WRITER_ROLES)
    return await svc.review_exception(db, user, a.case_id, "resolve", a.note)


async def _escalate(db, user, a):
    _require(user, WRITER_ROLES)
    return await svc.review_exception(db, user, a.case_id, "escalate", a.note)


async def _reject(db, user, a):
    _require(user, WRITER_ROLES)
    return await svc.review_exception(db, user, a.case_id, "reject", a.note)


async def _override(db, user, a):
    """Maker-checker aware: material overrides land in pending_approval."""
    _require(user, WRITER_ROLES)
    return await svc.review_exception(db, user, a.case_id, "override", a.note)


async def _decide_override(db, user, a):
    _require(user, CHECKER_ROLES)
    return await svc.decide_override(db, user, a.case_id, a.approve, a.note)


async def _review_match(db, user, a):
    _require(user, WRITER_ROLES)
    return await svc.review_match(db, user, a.decision_id, a.action, a.note)


async def _bulk(db, user, a: BulkExceptionsArgs):
    _require(user, WRITER_ROLES)
    return await svc.bulk_review_exceptions(db, user, a.batch_id, a.action,
                                            taxonomy=a.taxonomy, note=a.note)


async def _sandbox(db, user, a, process_batch_fn=None, generate_fn=None):
    _require(user, CHECKER_ROLES)
    return await svc.create_sandbox_batch(db, user, process_batch_fn, generate_fn)


async def _rerun(db, user, a, process_batch_fn=None):
    _require(user, WRITER_ROLES)
    return await svc.rerun_batch(db, user, a.batch_id, process_batch_fn)


async def _schedule(db, user, a: CreateScheduleArgs, next_fire_fn=None):
    _require(user, CHECKER_ROLES)
    return await svc.create_schedule(db, user, a.name, a.cron, a.action,
                                     next_fire_fn=next_fire_fn)


async def _policy(db, user, a: CreatePolicyArgs):
    _require(user, CHECKER_ROLES)
    return await svc.create_policy_version(db, user, a.amount_tolerance_paise,
                                           a.timing_lag_days,
                                           a.auto_post_confidence, note=a.note)


# ---------------------------------------------------------------- registry
# entries: (runner, args_model, allowed_roles, description, needs)
ACTIONS = {
    "resolve_exception": (
        _resolve, ResolveExceptionArgs, WRITER_ROLES,
        "Resolve an open exception case (marks it resolved with your note)."),
    "escalate_exception": (
        _escalate, ResolveExceptionArgs, WRITER_ROLES,
        "Escalate an exception case to bank-ops follow-up."),
    "reject_exception": (
        _reject, ResolveExceptionArgs, WRITER_ROLES,
        "Reject an exception case as not actionable."),
    "override_exception": (
        _override, OverrideArgs, WRITER_ROLES,
        "Override an exception outcome. Material value (>₹2,000) auto-routes to "
        "pending_approval for a controller/admin checker."),
    "decide_override": (
        _decide_override, DecideOverrideArgs, CHECKER_ROLES,
        "Checker sign-off: approve or reject an override that is pending_approval."),
    "review_match_decision": (
        _review_match, ReviewMatchArgs, WRITER_ROLES,
        "Approve/reject/escalate a workbench match decision."),
    "bulk_review_exceptions": (
        _bulk, BulkExceptionsArgs, WRITER_ROLES,
        "Bulk resolve/escalate/reject every open exception in a batch, optionally scoped to one taxonomy."),
    "run_sandbox_batch": (
        None, RunSandboxArgs, CHECKER_ROLES,
        "Create a truth-labelled sandbox fixture batch (evaluation data)."),
    "rerun_batch": (
        None, RerunBatchArgs, WRITER_ROLES,
        "Re-run a batch's source under current policy (creates parent-linked rerun)."),
    "create_batch_schedule": (
        None, CreateScheduleArgs, CHECKER_ROLES,
        "Create a cron schedule that runs ingestion actions automatically."),
    "create_policy_version": (
        None, CreatePolicyArgs, CHECKER_ROLES,
        "Publish a new matching-policy version (tolerance paise, timing lag days)."),
}


def build_action_catalog():
    lines = []
    for name, (_fn, argm, roles, desc) in ACTIONS.items():
        params = ", ".join(f"{f}:{f_.annotation.__name__ if hasattr(f_.annotation, '__name__') else 'any'}"
                           for f, f_ in argm.model_fields.items())
        lines.append(f"- {name} [roles: {'/'.join(roles)}]: {desc} Args: {{{params}}}")
    return "\n".join(lines)
