"""Agentic Copilot orchestrator v2 — a real ReAct loop.

The LLM drives every step: it decides which tool to call next (read-only
investigation OR state-changing actions), observes the result, and continues
until it produces a grounded final answer or hits the step cap.

Hard properties:
  - NO deterministic fallback. Without a configured provider this raises
    ProviderNotConfigured (the API surfaces HTTP 503). The agent is an agent
    or it is nothing.
  - Every tool call — read or write — executes through the same validated,
    RBAC-enforced service layer the UI uses. Actions are audit-logged by the
    services themselves, so agent-initiated changes are indistinguishable
    from human-initiated ones in the immutable trail.
  - Final answers pass contract validation + citation groundedness guard;
    fabricated references are stripped and reported.
"""
import asyncio
import json
import time

from pydantic import ValidationError

from . import providers, action_tools
from .contracts import CopilotAnswer, validate_copilot
from .tools import TOOLS as READ_TOOLS, RunContext, tool_catalog_for_prompt
from .evidence import _TOKEN_SPLIT  # noqa: F401  (kept import-stable)


class ProviderNotConfigured(RuntimeError):
    pass


MAX_STEPS = 8
OBSERVATION_CHARS = 1300
TRANSCRIPT_CHARS = 26000


def _clip(obj, limit):
    s = json.dumps(obj, default=str)
    return obj if len(s) <= limit else {"_truncated": True, "preview": s[:limit]}


def _trim_lists(obj, cap):
    if isinstance(obj, dict):
        return {k: _trim_lists(v, cap) for k, v in obj.items()}
    if isinstance(obj, list):
        out = [_trim_lists(x, cap) for x in obj[:cap]]
        if len(obj) > cap:
            out.append({"_omitted": len(obj) - cap})
        return out
    return obj


def _shrink(obj, limit=1400):
    """Structural clip: shrink lists before ever string-truncating."""
    if len(json.dumps(obj, default=str)) <= limit:
        return obj
    for cap in (12, 8, 4, 2, 1):
        t = _trim_lists(obj, cap)
        if len(json.dumps(t, default=str)) <= limit:
            return t
    return {"_truncated": True,
            "preview": json.dumps(obj, default=str)[:limit]}


AGENT_SYSTEM = (
    "You are the Finance Agent embedded in a Razorpay-style reconciliation "
    "control tower. You fulfill analyst requests end-to-end: investigate "
    "reconciled data AND execute operational actions on their behalf.\n\n"
    "CAPABILITIES (read-only):\n{read_tools}\n\n"
    "CAPABILITIES (actions — you change real state, audited under the "
    "operator's identity):\n{action_tools}\n\n"
    "OPERATOR CONTEXT:\n{context}\n\n"
    "RULES:\n"
    "- Fulfil the EXACT request. Investigate first when details are unknown "
    "(find ids via search/query tools before acting on them).\n"
    "- Actions are irreversible-ish state changes; act only when the request "
    "clearly intends them, and say what you did.\n"
    "- If a call fails (bad id, missing permission), read the error, adapt or "
    "explain — never invent ids.\n"
    "- Cite exact settlement_ids/UTRs in the final answer; fabricated "
    "citations are stripped automatically.\n\n"
    "PROTOCOL — respond ONLY with one JSON object:\n"
    '{{"action":"tool","tool":"<name>","args":{{...}},"thought":"one line"}}\n'
    'or\n'
    '{{"action":"final","answer":"markdown ok","cited_records":["ids"],'
    '"suggested_next_action":"one line"}}'
)


def _harvest_ids(obj, sink):
    if isinstance(obj, dict):
        for k, v in obj.items():
            if k in ("settlement_id", "utr", "statement_utr", "id") \
                    and isinstance(v, str) and len(v) >= 6:
                sink.add(v)
                sink.add(v.upper())
            else:
                _harvest_ids(v, sink)
    elif isinstance(obj, list):
        for item in obj:
            _harvest_ids(item, sink)


def _extract_json(raw):
    try:
        start, end = raw.index("{"), raw.rindex("}") + 1
        return json.loads(raw[start:end])
    except (ValueError, json.JSONDecodeError):
        return None


class AgentLoop:
    def __init__(self, db, user, ctx: RunContext, deps: dict):
        self.db, self.user, self.ctx, self.deps = db, user, ctx, deps
        self.known_ids = set()
        self.trace = []
        self.transcript = []

    def _system_prompt(self):
        role = self.user.get("role")
        actions = "\n".join(
            f"- {name}: {desc}"
            for name, (_fn, _am, roles, desc) in action_tools.ACTIONS.items()
            if role in roles)
        context = {
            "operator_role": role,
            "default_batch_id": self.ctx.get("default_batch_id"),
            "attachments": [{"name": a.name, "kind": a.kind, "rows": a.row_count}
                            for a in self.ctx.attachments],
        }
        return AGENT_SYSTEM.format(
            read_tools=tool_catalog_for_prompt(),
            action_tools=actions or "(none available for this role)",
            context=providers.scrub(json.dumps(context)))

    async def _run_read_tool(self, name, args_dict):
        fn, argm, _desc = READ_TOOLS[name]
        args = argm(**(args_dict or {}))
        data = await asyncio.wait_for(fn(self.db, args, self.ctx), timeout=15)
        _harvest_ids(data, self.known_ids)
        return {"ok": True, "data": _shrink(data)}

    async def _run_action_tool(self, name, args_dict):
        _fn, argm, roles, _desc = action_tools.ACTIONS[name]
        if self.user.get("role") not in roles:
            raise PermissionError(
                f"{name} requires role {'or'.join(roles)}; you are {self.user.get('role')}")
        args = argm(**(args_dict or {}))
        if name == "run_sandbox_batch":
            runner = action_tools._sandbox
            data = await runner(self.db, self.user, args,
                                process_batch_fn=self.deps["process_batch"],
                                generate_fn=self.deps["generate_batch"])
        elif name == "rerun_batch":
            data = await action_tools._rerun(
                self.db, self.user, args,
                process_batch_fn=self.deps["process_batch"])
        elif name == "create_batch_schedule":
            data = await action_tools._schedule(
                self.db, self.user, args, next_fire_fn=self.deps["next_fire"])
        else:
            data = await _fn(self.db, self.user, args)
        _harvest_ids(data, self.known_ids)
        return {"ok": True, "result": _clip(data, 700), "state_changed": True}

    async def execute(self, name, args_dict):
        sig = (name, json.dumps(args_dict or {}, sort_keys=True))
        if sig == getattr(self, "_last_sig", None):
            return {"ok": False,
                    "error": "identical call repeated; vary the arguments or use a different tool"}
        self._last_sig = sig
        t0 = time.perf_counter()
        try:
            if name in READ_TOOLS:
                res = await self._run_read_tool(name, args_dict)
            elif name in action_tools.ACTIONS:
                res = await self._run_action_tool(name, args_dict)
            else:
                res = {"ok": False, "error": f"unknown tool {name!r}"}
                import metrics as _metrics_mod
                _metrics_mod.record_agent_tool(name, False)
                return res
            res["ms"] = round((time.perf_counter() - t0) * 1000, 1)
            import metrics as _metrics_mod
            _metrics_mod.record_agent_tool(name, True, res.get("state_changed", False))
            return res
        except ValidationError as e:
            first = e.errors()[0]
            loc = ".".join(map(str, first.get("loc", []))) or "args"
            return {"ok": False, "error": f"invalid args [{loc}]: {first.get('msg')}"}
        except PermissionError as e:
            return {"ok": False, "error": f"permission denied: {e}"}
        except LookupError as e:
            return {"ok": False, "error": str(e)}
        except ValueError as e:
            return {"ok": False, "error": str(e)}
        except asyncio.TimeoutError:
            return {"ok": False, "error": "tool timed out"}
        except Exception as e:  # noqa: BLE001
            return {"ok": False, "error": f"{e.__class__.__name__}: {str(e)[:160]}"}

    def observe(self, step_no, name, thought, result):
        self.trace.append({
            "step": step_no, "tool": name,
            "ok": result.get("ok", False),
            "ms": result.get("ms"),
            "error": result.get("error"),
            "state_changed": result.get("state_changed", False),
            "summary": _clip(result.get("data") or result.get("result")
                             or result.get("error"), 400),
        })
        obs = json.dumps(result, default=str)[:OBSERVATION_CHARS]
        self.transcript.append(f"TOOL CALL #{step_no}: {name}\nOBSERVATION: {obs}")
        while sum(len(t) for t in self.transcript) > TRANSCRIPT_CHARS and len(self.transcript) > 3:
            self.transcript.pop(1)

    def guard(self, answer_data):
        kept, dropped = [], []
        for c in answer_data.get("cited_records", []) or []:
            (kept if str(c).upper() in {k.upper() for k in self.known_ids}
             else dropped).append(str(c))
        answer_data["cited_records"] = kept
        if dropped:
            answer_data.setdefault("failed_checks", []).append(
                f"citations removed by groundedness guard: {dropped}")
        return answer_data


async def run_agent_question(db, question, ctx: RunContext, user, deps: dict,
                             history=None):
    """ReAct loop entrypoint. Raises ProviderNotConfigured without a key.

    history: prior (role, text) turns for session memory."""
    if providers._SEND is None:
        raise ProviderNotConfigured(
            "No LLM provider configured. Set ANTHROPIC_API_KEY (or OPENAI_API_KEY) "
            "in backend/.env and restart.")

    loop = AgentLoop(db, user, ctx, deps)
    t_start = time.perf_counter()
    system = loop._system_prompt()
    if history:
        compact = "\n".join(f"{'USER' if r == 'user' else 'ASSISTANT'}: {t[:300]}"
                            for r, t in history)
        loop.transcript.append(f"PRIOR CONVERSATION:\n{compact}")
    loop.transcript.append(f"USER REQUEST: {providers.scrub(question)}")

    final_payload = None
    invocations = []
    provider_fault = None

    for step_no in range(1, MAX_STEPS + 1):
        prompt = "\n\n".join(loop.transcript) + \
            "\n\nYour next JSON decision:"
        try:
            raw = await providers.complete(system, prompt, timeout_s=240)
        except Exception as e:  # noqa: BLE001 — provider faults must not 500
            provider_fault = f"{e.__class__.__name__}: {str(e)[:160]}"
            break
        invocations.append(_invocation("agent_loop", prompt[-500:], raw,
                                       raw is not None))
        if not raw:
            provider_fault = "LLM provider returned an empty response"
            break

        decision = _extract_json(raw)
        if decision is None or "action" not in decision:
            loop.transcript.append(
                'SYSTEM: unparseable reply. Respond ONLY with '
                '{"action":"tool",...} or {"action":"final",...}.')
            continue

        if decision.get("action") == "final":
            parsed, err = validate_copilot(json.dumps(decision))
            if parsed is None:
                loop.transcript.append(f"SYSTEM: final answer rejected ({err}). "
                                       "Fix and resend final JSON.")
                continue
            final_payload = loop.guard(parsed.model_dump())
            break

        if decision.get("action") != "tool":
            loop.transcript.append('SYSTEM: action must be "tool" or "final".')
            continue

        name = decision.get("tool")
        thought = str(decision.get("thought", ""))[:200]
        result = await loop.execute(name, decision.get("args") or {})
        loop.observe(step_no, name, thought, result)

    elapsed_ms = round((time.perf_counter() - t_start) * 1000, 1)

    if final_payload is None:
        executed = [t for t in loop.trace if t["ok"]]
        changed = [t["tool"] for t in executed if t.get("state_changed")]
        checks = []
        if provider_fault:
            checks.append(f"provider interrupted the run: {provider_fault}")
        else:
            checks.append("step budget exhausted before final answer")
        final_payload = {
            "answer": ("I stopped early before producing a final synthesis. "
                       "Completed steps:\n" +
                       "\n".join(f"- {t['tool']}: {'ok' if t['ok'] else t['error']}"
                                 for t in loop.trace[-5:])),
            "cited_records": list(loop.known_ids)[:12],
            "failed_checks": checks,
            "suggested_next_action": "Ask me to continue from these findings."
            if executed else "Rephrase the request.",
        }
        if changed:
            final_payload["failed_checks"].append(
                f"actions already applied during run: {changed}")

    payload = {
        "mode": "agentic-loop",
        "provider": providers.PROVIDER_LABEL,
        "plan": [{"step": t["step"], "tool": t["tool"], "ok": t["ok"],
                  "ms": t.get("ms"), "error": t.get("error"),
                  "state_changed": t.get("state_changed", False)}
                 for t in loop.trace],
        "attachments": [{"name": a.name, "kind": a.kind, "rows": a.row_count,
                         "columns": a.columns[:18]} for a in ctx.attachments],
        "latency_ms": elapsed_ms,
        **final_payload,
    }
    return payload, invocations


def _invocation(agent, prompt, raw_output, used_llm):
    from .runtime import _record
    return _record(agent, prompt, raw_output, bool(used_llm), 0.0,
                   validated=bool(used_llm), repaired=False)
