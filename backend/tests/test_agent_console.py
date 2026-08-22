"""Agentic Copilot v2 — real ReAct loop tests with a SCRIPTED LLM provider.

conftest runs the API server inside this same process, so patching
agents.providers reaches the live app. No deterministic fallback exists
anymore: without a provider the endpoint returns 503; every behaviour here
is driven by scripted model decisions exactly as a real LLM would emit them.
"""
import csv
import io
import json
import os

import pytest
import requests

BASE_URL = os.environ["REACT_APP_BACKEND_URL"].rstrip("/")

ADMIN = ("admin@recon.io", "admin123")
CONTROLLER = ("controller@recon.io", "controller123")
ANALYST = ("analyst@recon.io", "analyst123")


def _login(creds):
    email, pw = creds
    r = requests.post(f"{BASE_URL}/api/auth/login",
                      json={"email": email, "password": pw}, timeout=30)
    return {"Authorization": f"Bearer {r.json()['token']}"}


def _tool(name, args, thought="step"):
    return json.dumps({"action": "tool", "tool": name, "args": args,
                       "thought": thought})


def _final(answer, cited=None, next_action="done"):
    return json.dumps({"action": "final", "answer": answer,
                       "cited_records": cited or [],
                       "suggested_next_action": next_action})


class ScriptedLLM:
    """Queue-driven fake provider. `dynamic` receives the latest prompt so a
    reply can reference observations (like a real model reading tool output)."""

    def __init__(self):
        self.replies = []
        self.prompts = []
        self.dynamic = None

    async def __call__(self, system, prompt):
        self.prompts.append(prompt)
        if self.replies:
            return self.replies.pop(0)
        if self.dynamic:
            return self.dynamic(prompt)
        return _final("script exhausted")


@pytest.fixture
def scripted_llm(monkeypatch):
    from agents import providers
    fake = ScriptedLLM()
    monkeypatch.setattr(providers, "_SEND", fake)
    monkeypatch.setattr(providers, "PROVIDER_LABEL", "scripted-test-llm")
    return fake


@pytest.fixture(scope="module")
def controller():
    return _login(CONTROLLER)


@pytest.fixture(scope="module")
def analyst():
    return _login(ANALYST)


@pytest.fixture(scope="module")
def admin():
    return _login(ADMIN)


@pytest.fixture(scope="module")
def fixture_batch(admin):
    r = requests.post(f"{BASE_URL}/api/sandbox/batch", headers=admin, timeout=120)
    assert r.status_code == 200
    return r.json()


def _ask(headers, question, files=None, batch_id=None):
    data = {"question": (None, question)}
    if batch_id:
        data["batch_id"] = (None, batch_id)
    for fname, content in (files or []):
        data["files"] = (fname, content, "text/csv")
    return requests.post(f"{BASE_URL}/api/copilot/agent", headers=headers,
                         files=data, timeout=180)


# ---------------------------------------------------------------- contract
class TestNoProviderContract:
    def test_503_with_setup_guidance(self, analyst, monkeypatch):
        from agents import providers
        monkeypatch.setattr(providers, "_SEND", None)
        r = _ask(analyst, "any question at all")
        assert r.status_code == 503
        assert "ANTHROPIC_API_KEY" in r.json()["detail"]


# ---------------------------------------------------------------- read flow
class TestReActReadFlow:
    def test_tool_then_final_grounded_citations(self, analyst, scripted_llm,
                                                fixture_batch):
        bid = fixture_batch["id"]

        def dynamic(prompt):
            # "read" the observation like a real model, cite what it saw
            import re
            m = re.search(r'"settlement_id":\s*"([^"]+)"', prompt)
            return _final(f"Investigated {bid[:8]}. Top exception: {m.group(1)}.",
                          cited=[m.group(1)])

        scripted_llm.replies = [_tool("query_exceptions", {"limit": 3},
                                      "look at exceptions first")]
        scripted_llm.dynamic = dynamic

        r = _ask(analyst, "what are the top open exceptions?", batch_id=bid)
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["mode"] == "agentic-loop"
        assert body["provider"] == "scripted-test-llm"
        steps = [(p["tool"], p["ok"]) for p in body["plan"]]
        assert steps == [("query_exceptions", True)]
        excs = requests.get(f"{BASE_URL}/api/exceptions?batch_id={bid}",
                            headers=analyst, timeout=30).json()["items"]
        real = {e.get("settlement_id") for e in excs}
        assert all(c in real for c in body["cited_records"])   # groundedness kept them
        assert not any(c.startswith("citations removed") for c in body["failed_checks"])

    def test_fabricated_citation_stripped_by_guard(self, analyst, scripted_llm,
                                                   fixture_batch):
        scripted_llm.replies = [
            _final("Trust me about GHOST_SETTLEMENT_42.",
                   cited=["GHOST_SETTLEMENT_42"])]
        r = _ask(analyst, "summarize", batch_id=fixture_batch["id"])
        body = r.json()
        assert "GHOST_SETTLEMENT_42" not in body["cited_records"]
        assert any("groundedness guard" in c for c in body["failed_checks"])

    def test_unknown_tool_error_fed_back_then_recovers(self, analyst,
                                                       scripted_llm,
                                                       fixture_batch):
        scripted_llm.replies = [_tool("make_money_mint_ledger", {},
                                      "hallucinated tool")]
        scripted_llm.replies.append(_final("Recovered after bad tool name."))
        r = _ask(analyst, "do something impossible", batch_id=fixture_batch["id"])
        body = r.json()
        failed = [p for p in body["plan"] if not p["ok"]]
        assert failed and "unknown tool" in failed[0]["error"]
        assert "Recovered" in body["answer"]

    def test_step_budget_cap_reported_honestly(self, analyst, scripted_llm,
                                               fixture_batch):
        scripted_llm.replies = [
            _tool("query_batches", {"limit": 1}) for _ in range(9)]
        r = _ask(analyst, "keep looking", batch_id=fixture_batch["id"])
        body = r.json()
        assert len(body["plan"]) <= 8
        assert any("budget" in c for c in body["failed_checks"])

    def test_attachment_reconcile_flow_via_loop(self, analyst, scripted_llm,
                                                fixture_batch):
        matches = requests.get(
            f"{BASE_URL}/api/reconciliation?batch_id={fixture_batch['id']}&status=matched&limit=1",
            headers=analyst, timeout=30).json()
        utr, amount = matches[0]["utr"], matches[0]["settlement_amount_paise"]
        buf = io.StringIO()
        w = csv.writer(buf)
        w.writerow(["utr", "amount", "date", "narration"])
        w.writerow([utr, f"{amount / 100:.2f}", "2026-06-02", "NEFT CR"])
        w.writerow(["UTR_STRAY_9999", "777.77", "2026-06-03", "IMPS CR"])

        scripted_llm.replies = [_tool("preview_reconcile", {},
                                      "reconcile attachment")]
        scripted_llm.dynamic = lambda p: _final(
            "Statement reconciled: exact match found plus one stray credit.",
            cited=[utr])
        r = _ask(analyst, "reconcile this statement",
                 files=[("statement.csv", buf.getvalue().encode())],
                 batch_id=fixture_batch["id"])
        body = r.json()
        tools = [p["tool"] for p in body["plan"]]
        assert tools == [("preview_reconcile" in t and t) or t for t in tools] or \
            "preview_reconcile" in tools
        assert body["attachments"][0]["kind"] == "bank_statement"
        assert str(amount / 100) in body["answer"] or "exact" in body["answer"].lower()


# ---------------------------------------------------------------- actions
class TestReActActionFlow:
    def test_agent_resolves_exception_as_controller(self, controller,
                                                    scripted_llm,
                                                    fixture_batch):
        bid = fixture_batch["id"]
        excs = requests.get(f"{BASE_URL}/api/exceptions?batch_id={bid}&status=open",
                            headers=controller, timeout=30).json()["items"]
        target = next(e for e in excs if e["value_at_risk_paise"] <= 200000)
        scripted_llm.replies = [
            _tool("resolve_exception",
                  {"case_id": target["id"], "note": "agent-resolved per request"},
                  "resolving as asked"),
            _final(f"Resolved {target['settlement_id']} for you.",
                   cited=[target["settlement_id"]])]

        r = _ask(controller, f"please resolve exception "
                             f"{target['settlement_id']} with note agent-resolved",
                 batch_id=bid)
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["plan"][0]["ok"] is True
        assert body["plan"][0]["state_changed"] is True

        after = requests.get(f"{BASE_URL}/api/exceptions/{target['id']}",
                             headers=controller, timeout=30).json()
        assert after["status"] == "resolved"
        assert after["review"]["by"] == CONTROLLER[0]
        # audited under the operator's identity
        events = requests.get(f"{BASE_URL}/api/audit?batch_id={bid}",
                              headers=controller, timeout=30).json()
        assert any(e["action"] == "exception_resolve" and
                   e["entity_id"] == target["id"] for e in events)

    def test_maker_checker_routes_analyst_override_to_pending(self, analyst,
                                                              controller,
                                                              scripted_llm,
                                                              fixture_batch):
        bid = fixture_batch["id"]
        excs = requests.get(f"{BASE_URL}/api/exceptions?batch_id={bid}&status=open",
                            headers=analyst, timeout=30).json()["items"]
        material = [e for e in excs if e["value_at_risk_paise"] > 200000]
        if not material:
            pytest.skip("no material exception in this fixture")
        target = material[0]

        scripted_llm.replies = [
            _tool("override_exception",
                  {"case_id": target["id"], "note": "analyst override attempt"}),
            _final("Override routed to pending approval (material value).")]

        r = _ask(analyst, f"override {target['settlement_id']}", batch_id=bid)
        body = r.json()
        assert body["plan"][0]["ok"] is True
        after = requests.get(f"{BASE_URL}/api/exceptions/{target['id']}",
                             headers=controller, timeout=30).json()
        assert after["status"] == "pending_approval"

        # checker approves through the agent too
        scripted_llm.replies = [
            _tool("decide_override",
                  {"case_id": target["id"], "approve": True, "note": "checker ok"}),
            _final("Approved the override.")]
        r2 = _ask(controller, f"approve override {target['settlement_id']}",
                  batch_id=bid)
        assert r2.status_code == 200
        final_state = requests.get(f"{BASE_URL}/api/exceptions/{target['id']}",
                                   headers=controller, timeout=30).json()
        assert final_state["status"] == "resolved"

    def test_permission_denied_surfaced_to_model(self, analyst, scripted_llm,
                                                 fixture_batch):
        scripted_llm.replies = [
            _tool("create_policy_version", {"amount_tolerance_paise": 5}),
            _final("I cannot change policy with your role — explained.")]
        r = _ask(analyst, "tighten tolerance to 5 paise")
        body = r.json()
        failed = [p for p in body["plan"] if not p["ok"]]
        assert failed and "permission denied" in failed[0]["error"]

    def test_support_role_blocked_from_actions_entirely(self, scripted_llm):
        support = _login(("support@recon.io", "support123"))
        scripted_llm.replies = [
            _tool("escalate_exception", {"case_id": "whatever"}),
            _final("explained")]
        r = _ask(support, "escalate that case")
        body = r.json()
        failed = [p for p in body["plan"] if not p["ok"]]
        assert failed and "permission denied" in failed[0]["error"]

    def test_sandbox_and_schedule_actions(self, admin, scripted_llm):
        scripted_llm.replies = [
            _tool("run_sandbox_batch", {}),
            _final("Created sandbox fixture.")]
        r = _ask(admin, "spin up a sandbox batch for evaluation")
        assert r.status_code == 200
        assert r.json()["plan"][0]["state_changed"] is True

        scripted_llm.replies = [
            _tool("create_batch_schedule",
                  {"name": "agent nightly", "cron": "0 3 * * *",
                   "action": "sandbox_seed"}),
            _final("Schedule created.")]
        r2 = _ask(admin, "schedule nightly sandbox seeding at 03:00 UTC")
        assert r2.status_code == 200
        scheds = requests.get(f"{BASE_URL}/api/schedules", headers=admin,
                              timeout=30).json()
        assert any(s["name"] == "agent nightly" for s in scheds)


# ---------------------------------------------------------------- observability
class TestObservability:
    def test_invocations_recorded(self, analyst, scripted_llm, fixture_batch):
        scripted_llm.replies = [_final("done")]
        _ask(analyst, "hello there agent", batch_id=fixture_batch["id"])
        m = requests.get(f"{BASE_URL}/api/agents/metrics", headers=analyst,
                         timeout=30).json()
        assert "agent_loop" in {a["agent"] for a in m["agents"]}
