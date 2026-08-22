"""Agent runtime: invoke -> validate -> bounded repair -> record.

Guarantees:
  - At most one corrective re-ask when the first reply fails its contract.
  - Every call (success, repair, or fallback) emits a model_invocation record
    with latency/validation metadata so agent accuracy is measurable over time.
  - The runtime never decides outcomes; it returns validated proposals only.
"""
import time
import uuid
from datetime import datetime, timezone

from . import providers


def _record(agent, prompt, raw_output, used_llm, latency_ms, validated, repaired,
            error=None):
    return {
        "id": str(uuid.uuid4()),
        "agent": agent,
        "model": providers.PROVIDER_LABEL if used_llm else "deterministic",
        "prompt_preview": (providers.scrub(prompt) or "")[:500],
        "output_preview": (providers.scrub(raw_output) if raw_output else "")[:1500],
        "created_at": datetime.now(timezone.utc).isoformat(),
        "latency_ms": round(latency_ms, 1),
        "validated": bool(validated),
        "repaired": bool(repaired),
        "fallback": not used_llm,
        "validation_error": error,
    }


async def invoke_json(agent, system, prompt, validator):
    """Run one JSON-contract agent turn.

    Returns (validated_model_or_None, invocation_record).
    """
    t0 = time.perf_counter()
    raw = await providers.complete(system, prompt)
    obj, err = validator(raw)

    repaired = False
    if obj is None and raw:
        # one bounded self-repair round with the specific contract failure named
        repair_prompt = (
            f"{prompt}\n\nYour previous reply violated the required output contract: {err}. "
            "Return ONLY a corrected JSON object matching the schema. No prose.")
        raw2 = await providers.complete(system, repair_prompt)
        obj, err2 = validator(raw2)
        repaired = True
        if obj is not None:
            err = None
            raw = raw2

    latency = (time.perf_counter() - t0) * 1000
    return obj, _record(agent, prompt, raw, obj is not None, latency,
                        obj is not None, repaired, err)


async def invoke_text(agent, system, prompt):
    """Free-text agent turn (reviewer copilot). Returns (text|None, invocation_record).

    `validated` is True only when the provider produced the text; deterministic
    fallbacks are recorded as such so acceptance metrics stay honest.
    """
    t0 = time.perf_counter()
    raw = await providers.complete(system, prompt)
    text = raw.strip() if raw and raw.strip() else None
    latency = (time.perf_counter() - t0) * 1000
    return (text or "") , _record(agent, prompt, raw, text is not None, latency,
                                  text is not None, False,
                                  None if text else "empty output")
