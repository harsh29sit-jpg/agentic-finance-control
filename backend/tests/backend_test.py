"""End-to-end backend API tests for the Razorpay Reconciliation Control Tower."""
import os
import time
import pytest
import requests

BASE_URL = os.environ.get("REACT_APP_BACKEND_URL", "").rstrip("/")
if not BASE_URL:
    # Read from frontend/.env directly if not exported
    with open("/app/frontend/.env") as f:
        for line in f:
            if line.startswith("REACT_APP_BACKEND_URL="):
                BASE_URL = line.split("=", 1)[1].strip().rstrip("/")
                break

USERS = {
    "analyst":    ("analyst@recon.io", "analyst123"),
    "controller": ("controller@recon.io", "controller123"),
    "compliance": ("compliance@recon.io", "compliance123"),
    "admin":      ("admin@recon.io", "admin123"),
    "support":    ("support@recon.io", "support123"),
}


# ---------- shared session-scope fixtures ----------
@pytest.fixture(scope="session")
def tokens():
    out = {}
    for role, (email, pw) in USERS.items():
        r = requests.post(f"{BASE_URL}/api/auth/login",
                          json={"email": email, "password": pw}, timeout=30)
        assert r.status_code == 200, f"login {role} failed: {r.status_code} {r.text}"
        data = r.json()
        assert "token" in data or "access_token" in data, data
        out[role] = data.get("token") or data.get("access_token")
    return out


def _h(tok):
    return {"Authorization": f"Bearer {tok}"}


@pytest.fixture(scope="session")
def demo_batch(tokens):
    r = requests.post(f"{BASE_URL}/api/batches/run-demo",
                      headers=_h(tokens["analyst"]), timeout=120)
    assert r.status_code == 200, f"run-demo failed: {r.status_code} {r.text}"
    body = r.json()
    # batch_id may be nested; find it
    batch_id = body.get("batch_id") or body.get("id") or (body.get("batch") or {}).get("id")
    assert batch_id, f"No batch id in response: {body}"
    return {"batch_id": batch_id, "body": body}


# ---------- Auth ----------
class TestAuth:
    def test_login_all_roles(self, tokens):
        assert set(tokens.keys()) == set(USERS.keys())
        for role, t in tokens.items():
            assert isinstance(t, str) and len(t) > 10

    def test_me_endpoint(self, tokens):
        r = requests.get(f"{BASE_URL}/api/auth/me", headers=_h(tokens["analyst"]), timeout=15)
        assert r.status_code == 200
        me = r.json()
        assert me.get("email") == "analyst@recon.io"
        assert me.get("role") in ("analyst", "ANALYST")

    def test_invalid_credentials(self):
        r = requests.post(f"{BASE_URL}/api/auth/login",
                          json={"email": "analyst@recon.io", "password": "wrongpw"}, timeout=15)
        assert r.status_code in (400, 401, 403)

    def test_missing_token_rejected(self):
        r = requests.get(f"{BASE_URL}/api/auth/me", timeout=15)
        assert r.status_code in (401, 403)


# ---------- RBAC ----------
class TestRBAC:
    def test_support_cannot_run_batch(self, tokens):
        r = requests.post(f"{BASE_URL}/api/batches/run-demo",
                          headers=_h(tokens["support"]), timeout=30)
        assert r.status_code == 403, f"expected 403 got {r.status_code}: {r.text}"

    def test_analyst_cannot_publish_policy(self, tokens):
        payload = {"name": "test_policy", "tolerance_paise": 100, "rules": {}}
        r = requests.post(f"{BASE_URL}/api/policies",
                          headers=_h(tokens["analyst"]), json=payload, timeout=15)
        assert r.status_code == 403

    def test_controller_can_publish_policy(self, tokens):
        payload = {"name": "TEST_ctrl_policy", "tolerance_paise": 150,
                   "rules": {"pass2_band_paise": 150}}
        r = requests.post(f"{BASE_URL}/api/policies",
                          headers=_h(tokens["controller"]), json=payload, timeout=15)
        assert r.status_code in (200, 201), r.text


# ---------- Batch ----------
class TestBatchDemo:
    def test_run_demo_metrics(self, demo_batch):
        b = demo_batch["body"]
        metrics = b.get("metrics") or b
        # Look up metrics in various possible shapes
        for key in ["deterministic_match_rate", "inclusive_match_rate",
                    "false_match_rate", "exception_recall",
                    "reconciled_value_paise", "value_at_risk_paise",
                    "open_exceptions", "exceptions_by_taxonomy"]:
            assert key in metrics, f"metric {key} missing in {list(metrics.keys())}"
        assert metrics["false_match_rate"] == 0
        assert metrics["exception_recall"] == 100


# ---------- Reconciliation workbench ----------
class TestReconciliation:
    def test_list_matches(self, tokens, demo_batch):
        bid = demo_batch["batch_id"]
        r = requests.get(f"{BASE_URL}/api/reconciliation",
                         params={"batch_id": bid},
                         headers=_h(tokens["analyst"]), timeout=30)
        assert r.status_code == 200, r.text
        data = r.json()
        rows = data if isinstance(data, list) else data.get("items") or data.get("results") or []
        assert len(rows) > 0
        sample = rows[0]
        # Basic evidence shape check
        assert "pass_number" in sample or "pass" in sample
        assert "confidence" in sample or "match_confidence" in sample


# ---------- Exceptions ----------
class TestExceptions:
    def test_list_exceptions(self, tokens, demo_batch):
        bid = demo_batch["batch_id"]
        r = requests.get(f"{BASE_URL}/api/exceptions",
                         params={"batch_id": bid, "group_by": "taxonomy"},
                         headers=_h(tokens["analyst"]), timeout=30)
        assert r.status_code == 200, r.text
        data = r.json()
        assert data, "no exceptions returned"

    def _fetch_exceptions(self, tokens, bid):
        r = requests.get(f"{BASE_URL}/api/exceptions",
                         params={"batch_id": bid},
                         headers=_h(tokens["analyst"]), timeout=30)
        assert r.status_code == 200
        d = r.json()
        if isinstance(d, dict):
            # grouped -> flatten
            items = []
            for v in d.values():
                if isinstance(v, list):
                    items.extend(v)
            if items:
                return items
            # or it may have 'items'
            return d.get("items", [])
        return d

    def test_ai_analyze_exception(self, tokens, demo_batch):
        bid = demo_batch["batch_id"]
        items = self._fetch_exceptions(tokens, bid)
        assert items, "no exceptions to analyze"
        exc_id = items[0].get("id") or items[0].get("_id") or items[0].get("exception_id")
        assert exc_id
        r = requests.post(f"{BASE_URL}/api/exceptions/{exc_id}/analyze",
                          headers=_h(tokens["analyst"]), timeout=90)
        assert r.status_code == 200, f"AI analyze failed: {r.status_code} {r.text[:400]}"
        body = r.json()
        # Any of these keys indicate an AI response envelope
        assert any(k in body for k in ("ai", "triage", "suggested_action",
                                       "reviewer_explanation", "ai_analyzed"))


# ---------- Maker-Checker override ----------
class TestMakerChecker:
    def test_material_override_flow(self, tokens, demo_batch):
        bid = demo_batch["batch_id"]
        r = requests.get(f"{BASE_URL}/api/exceptions",
                         params={"batch_id": bid},
                         headers=_h(tokens["analyst"]), timeout=30)
        d = r.json()
        items = []
        if isinstance(d, dict):
            for v in d.values():
                if isinstance(v, list):
                    items.extend(v)
            if not items:
                items = d.get("items", [])
        else:
            items = d
        # Find a material exception
        material = None
        for it in items:
            v = it.get("value_at_risk_paise") or it.get("amount_paise") or 0
            if v and v > 200000:
                material = it
                break
        if not material:
            pytest.skip("No material exception (>200000 paise) in seeded set")
        exc_id = material.get("id") or material.get("_id")
        r2 = requests.post(f"{BASE_URL}/api/exceptions/{exc_id}/review",
                           headers=_h(tokens["analyst"]),
                           json={"action": "override", "reason": "TEST maker-checker"},
                           timeout=30)
        assert r2.status_code in (200, 202), r2.text
        body = r2.json()
        assert (body.get("status") == "pending_approval") or body.get("pending_approval") is True, body

        # Controller approves
        r3 = requests.post(f"{BASE_URL}/api/exceptions/{exc_id}/override-approval",
                           headers=_h(tokens["controller"]),
                           json={"approve": True, "reason": "TEST approve"},
                           timeout=30)
        assert r3.status_code == 200, r3.text

    def test_pending_review_list(self, tokens):
        r = requests.get(f"{BASE_URL}/api/review/pending",
                         headers=_h(tokens["controller"]), timeout=15)
        assert r.status_code == 200


# ---------- Copilot ----------
class TestCopilot:
    def test_copilot_ask(self, tokens, demo_batch):
        bid = demo_batch["batch_id"]
        r = requests.post(f"{BASE_URL}/api/copilot/ask",
                          headers=_h(tokens["analyst"]),
                          json={"question": "What is the reconciled value?", "batch_id": bid},
                          timeout=90)
        assert r.status_code == 200, r.text
        b = r.json()
        assert "answer" in b or "response" in b
        # optional grounded fields
        # cited_records / failed_checks / suggested_next_action may be present


# ---------- Reports ----------
class TestReports:
    def test_report(self, tokens, demo_batch):
        bid = demo_batch["batch_id"]
        r = requests.get(f"{BASE_URL}/api/reports/{bid}",
                         headers=_h(tokens["controller"]), timeout=30)
        assert r.status_code == 200, r.text
        b = r.json()
        for k in ("acceptance_gates", "exception_ledger"):
            assert k in b, f"missing {k} in report"


# ---------- Audit + model invocations ----------
class TestAudit:
    def test_audit_events(self, tokens, demo_batch):
        bid = demo_batch["batch_id"]
        r = requests.get(f"{BASE_URL}/api/audit",
                         params={"batch_id": bid},
                         headers=_h(tokens["compliance"]), timeout=30)
        assert r.status_code == 200
        events = r.json()
        assert events, "no audit events"

    def test_model_invocations(self, tokens):
        r = requests.get(f"{BASE_URL}/api/model-invocations",
                         headers=_h(tokens["admin"]), timeout=15)
        assert r.status_code == 200


# ---------- Policies ----------
class TestPolicies:
    def test_list_policies(self, tokens):
        r = requests.get(f"{BASE_URL}/api/policies",
                         headers=_h(tokens["analyst"]), timeout=15)
        assert r.status_code == 200
        b = r.json()
        assert b
