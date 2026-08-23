"""LLM transport layer.

Provider resolution (first available wins):
  1. ANTHROPIC_API_KEY + anthropic SDK
  2. OPENAI_API_KEY    + openai SDK
  3. None -> deterministic fallbacks (pipeline stays fully functional)

Every outbound prompt is scrubbed for PAN/card-like data. Every call is
timeout-guarded so a hung provider can never stall a batch or request.
"""
import os
import re

PAN_RE = re.compile(r"\b(?:\d[ -]?){13,19}\b")
DEFAULT_TIMEOUT_S = 12


def scrub(text):
    """Mask PAN/card-like digit sequences before anything leaves the process."""
    if not text:
        return text

    def _sub(m):
        digits = re.sub(r"\D", "", m.group(0))
        return f"{digits[:4]}-MASKED-{digits[-2:]}" if len(digits) >= 13 else m.group(0)

    return PAN_RE.sub(_sub, str(text))


def _resolve():
    # 0. Any OpenAI-compatible gateway (custom base URL) — covers self-hosted
    #    gateways and third-party providers with an OpenAI-shaped API.
    if os.environ.get("CUSTOM_LLM_BASE_URL") and (
            os.environ.get("CUSTOM_LLM_API_KEY") or
            os.environ.get("OPENAI_API_KEY")):
        try:
            import openai  # noqa: PLC0415
            base = os.environ["CUSTOM_LLM_BASE_URL"].rstrip("/")
            model = os.environ.get("CUSTOM_LLM_MODEL", "default")
            key = os.environ.get("CUSTOM_LLM_API_KEY") or os.environ["OPENAI_API_KEY"]

            async def send_custom(system, prompt):
                client = openai.AsyncOpenAI(api_key=key, base_url=f"{base}/v1")
                r = await client.chat.completions.create(
                    model=model, temperature=0,
                    messages=[{"role": "system", "content": system},
                              {"role": "user", "content": prompt}])
                return r.choices[0].message.content

            return send_custom, f"custom/{model}"
        except ImportError:
            pass

    if os.environ.get("ANTHROPIC_API_KEY"):
        try:
            import anthropic  # noqa: PLC0415
            model = os.environ.get("ANTHROPIC_MODEL", os.environ.get("LLM_MODEL", "claude-sonnet-4-6"))

            async def send_anthropic(system, prompt):
                client = anthropic.AsyncAnthropic(api_key=os.environ["ANTHROPIC_API_KEY"])
                msg = await client.messages.create(
                    model=model, max_tokens=1024, temperature=0,
                    system=system,
                    messages=[{"role": "user", "content": prompt}])
                return "".join(b.text for b in msg.content if getattr(b, "type", "") == "text")

            return send_anthropic, "anthropic"
        except ImportError:
            pass

    if os.environ.get("OPENAI_API_KEY"):
        try:
            import openai  # noqa: PLC0415
            model = os.environ.get("OPENAI_MODEL", "gpt-4o")

            async def send_openai(system, prompt):
                client = openai.AsyncOpenAI(api_key=key, base_url=f"{base}/v1")
                r = await client.chat.completions.create(
                    model=model, temperature=0,
                    # reasoning models spend budget on thinking before content;
                    # without an explicit ceiling the reply can come back empty
                    max_tokens=int(os.environ.get("CUSTOM_LLM_MAX_TOKENS", "6000")),
                    messages=[{"role": "system", "content": system},
                              {"role": "user", "content": prompt}])
                return r.choices[0].message.content

            return send_openai, "openai"
        except ImportError:
            pass

    return None, "deterministic-fallback"


_SEND, PROVIDER_LABEL = _resolve()


async def complete(system, prompt, timeout_s=DEFAULT_TIMEOUT_S):
    """One non-streaming completion. Returns raw text or None on any failure.

    Never raises: provider faults degrade to the caller's deterministic path.
    """
    if _SEND is None:
        return None
    import asyncio
    try:
        return await asyncio.wait_for(_SEND(scrub(system), scrub(prompt)), timeout=timeout_s)
    except Exception:  # noqa: BLE001 — timeout, rate limit, network, SDK errors
        return None
