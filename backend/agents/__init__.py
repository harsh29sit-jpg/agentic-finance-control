"""Agentic exception layer.

Bounded agents over a shared runtime: provider transport, typed output
contracts, one-round self-repair, deterministic evidence verification, and
per-call observability. Agents propose and explain; they never mutate final
match state — every outcome stays human-gated.

Public API (stable, used by server.py):
    triage_exception(case)                  -> (triage_dict, invocation)
    analyze_narration(case, candidates)     -> (link_dict, invocation)
    reviewer_explain(case, triage)          -> (text, invocation)
    copilot_answer(question, context)       -> (answer_dict, invocation)
"""
from .triage import run as triage_exception, triage_fallback as _triage_fallback  # noqa: F401
from .narration import run as analyze_narration  # noqa: F401
from .reviewer import run as reviewer_explain  # noqa: F401
from .copilot import run as copilot_answer  # noqa: F401
from .providers import PROVIDER_LABEL  # noqa: F401

__all__ = ["triage_exception", "analyze_narration", "reviewer_explain",
           "copilot_answer", "_triage_fallback", "PROVIDER_LABEL"]
