"""Backend tests for the 3 new features: bulk-triage, evaluation dashboard, rerun diff."""
import os
import pytest
import requests

BASE_URL = os.environ.get("REACT_APP_BACKEND_URL", "").rstrip("/")
if not BASE_URL:
    try:
        with open("/app/frontend/.env") as f:
            for line in f:
                if line.startswith("REACT_APP_BACKEND_URL="):
                    BASE_URL = line.split("=", 1)[1].strip().rstrip("/")
                    break
    except OSError:
        pass
if not BASE_URL:
    pytest.skip("No backend URL configured for integration tests", allow_module_level=True)

USERS = {
    "analyst":    ("analyst@recon.io", "analyst123"),
    "controller": ("controller@recon.io", "controller123"),
    "admin":      ("admin@recon.io", "admin123"),
    "support":    ("support@recon.io", "support123"),
}


def _h(tok): return {"Authorization": f"Bearer {tok}"}


@pytest.fixture(scope="session")
def tokens():
    out = {}
    for role, (email, pw) in USERS.items():
        r = requests.post(f"{BASE_URL}/api/auth/login",
                          json={"email": email, "password": pw}, timeout=30)
        assert r.status_code == 200, f"login {role} failed: {r.text}"
        out[role] = r.json().get("token") or r.json().get("access_token")
    return out


@pytest.fixture(scope="session")
def fresh_demo_batch(tokens):
    """Run a brand-new demo batch for these tests."""
    r = requests.post(f"{BASE_URL}/api/batches/run-demo",
                      headers=_h(tokens["analyst"]), timeout=120)
    assert r.status_code == 200, r.text
    return r.json()


# =========================================================
# 1) BULK TRIAGE
# =========================================================
class TestBulkTriage:
    def test_support_forbidden(self, tokens, fresh_demo_batch):
        r = requests.post(f"{BASE_URL}/api/exceptions/bulk-review",
                          headers=_h(tokens["support"]),
                          json={"batch_id": fresh_demo_batch["id"], "action": "resolve",
                                "note": "TEST", "taxonomy": "MISSING_IN_BANK"},
                          timeout=15)
        assert r.status_code == 403, f"expected 403 got {r.status_code}"

    def test_bulk_escalate_by_taxonomy(self, tokens, fresh_demo_batch):
        bid = fresh_demo_batch["id"]
        # Pick a taxonomy that actually has cases
        excs = requests.get(f"{BASE_URL}/api/exceptions",
                            params={"batch_id": bid},
                            headers=_h(tokens["analyst"])).json()
        items = excs.get("items") if isinstance(excs, dict) else excs
        assert items, "no exceptions"
        taxonomy = items[0]["taxonomy"]

        r = requests.post(f"{BASE_URL}/api/exceptions/bulk-review",
                          headers=_h(tokens["analyst"]),
                          json={"batch_id": bid, "action": "escalate",
                                "note": "TEST bulk escalate",
                                "taxonomy": taxonomy}, timeout=30)
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["ok"] is True
        assert body["status"] == "escalated"
        assert body["affected"] >= 1

        # verify persisted
        excs2 = requests.get(f"{BASE_URL}/api/exceptions",
                             params={"batch_id": bid, "taxonomy": taxonomy},
                             headers=_h(tokens["analyst"])).json()
        items2 = excs2.get("items") if isinstance(excs2, dict) else excs2
        assert all(i["status"] in ("escalated", "resolved", "rejected") for i in items2)

        # verify ONE bulk audit event written
        audit = requests.get(f"{BASE_URL}/api/audit",
                             params={"batch_id": bid},
                             headers=_h(tokens["admin"])).json()
        bulk_events = [e for e in audit if e["action"].startswith("bulk_")]
        assert bulk_events, "no bulk_* audit event written"

    def test_bulk_resolve_by_ids(self, tokens, fresh_demo_batch):
        bid = fresh_demo_batch["id"]
        excs = requests.get(f"{BASE_URL}/api/exceptions",
                            params={"batch_id": bid},
                            headers=_h(tokens["analyst"])).json()
        items = excs.get("items") if isinstance(excs, dict) else excs
        open_ids = [i["id"] for i in items if i["status"] in ("open", "escalated")][:2]
        if not open_ids:
            pytest.skip("no open/escalated cases left")
        r = requests.post(f"{BASE_URL}/api/exceptions/bulk-review",
                          headers=_h(tokens["controller"]),
                          json={"batch_id": bid, "action": "resolve",
                                "note": "TEST bulk resolve by ids",
                                "ids": open_ids}, timeout=30)
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["status"] == "resolved"
        assert body["affected"] == len(open_ids)

    def test_bulk_invalid_action(self, tokens, fresh_demo_batch):
        r = requests.post(f"{BASE_URL}/api/exceptions/bulk-review",
                          headers=_h(tokens["analyst"]),
                          json={"batch_id": fresh_demo_batch["id"],
                                "action": "banana", "note": ""}, timeout=15)
        assert r.status_code in (400, 422)


# =========================================================
# 2) EVALUATION DASHBOARD
# =========================================================
class TestBenchmark:
    def test_benchmark_demo_batch_perfect(self, tokens, fresh_demo_batch):
        bid = fresh_demo_batch["id"]
        r = requests.get(f"{BASE_URL}/api/benchmark/{bid}",
                         headers=_h(tokens["analyst"]), timeout=30)
        assert r.status_code == 200, r.text
        s = r.json()
        assert s["has_truth"] is True
        assert s["auto_match_precision"] == 100
        assert s["match_recall"] == 100
        assert s["exception_recall"] == 100
        assert s["f1_score"] == 100
        assert s["false_positive"] == 0
        assert s["false_match_rate"] == 0
        assert isinstance(s.get("false_matches"), list) and s["false_matches"] == []
        gates = s["gates"]
        assert gates["precision_ok"] and gates["exception_recall_ok"] and gates["false_match_ok"]

    def test_benchmark_all(self, tokens):
        r = requests.get(f"{BASE_URL}/api/benchmark",
                         headers=_h(tokens["analyst"]), timeout=30)
        assert r.status_code == 200
        arr = r.json()
        assert isinstance(arr, list)
        # every labelled batch must expose full score payload
        for s in arr:
            for k in ("auto_match_precision", "match_recall", "exception_recall",
                      "f1_score", "false_positive", "true_positive", "false_negative"):
                assert k in s

    def test_benchmark_unknown_batch(self, tokens):
        r = requests.get(f"{BASE_URL}/api/benchmark/does-not-exist",
                         headers=_h(tokens["analyst"]), timeout=15)
        assert r.status_code == 404


# =========================================================
# 3) RERUN DIFF
# =========================================================
class TestRerunDiff:
    def test_rerun_same_policy_zero_changes(self, tokens, fresh_demo_batch):
        bid = fresh_demo_batch["id"]
        r = requests.post(f"{BASE_URL}/api/batches/{bid}/rerun",
                          headers=_h(tokens["analyst"]), timeout=120)
        assert r.status_code == 200, r.text
        new = r.json()
        assert new["parent_batch_id"] == bid
        # diff same policy
        d = requests.get(f"{BASE_URL}/api/diff",
                         params={"base": bid, "compare": new["id"]},
                         headers=_h(tokens["analyst"]), timeout=30).json()
        assert d["total_changes"] == 0, f"expected 0 changes, got {d['total_changes']}: {d['changes'][:3]}"
        assert d["regressed"] == 0
        assert d["resolved"] == 0

    def test_stricter_policy_causes_regressions(self, tokens):
        """Publish stricter policy (tolerance=0) -> rerun -> expect regressed>0. Then restore."""
        # 1. seed a fresh base batch under current default policy (100 paise tol)
        base = requests.post(f"{BASE_URL}/api/batches/run-demo",
                             headers=_h(tokens["analyst"]), timeout=120).json()
        try:
            # 2. publish stricter policy
            pol = requests.post(f"{BASE_URL}/api/policies",
                                headers=_h(tokens["admin"]),
                                json={"amount_tolerance_paise": 0,
                                      "timing_lag_days": 1,
                                      "auto_post_confidence": 0.95,
                                      "note": "TEST strict"}, timeout=15)
            assert pol.status_code in (200, 201), pol.text

            # 3. rerun
            rr = requests.post(f"{BASE_URL}/api/batches/{base['id']}/rerun",
                               headers=_h(tokens["analyst"]), timeout=120).json()

            # 4. diff
            d = requests.get(f"{BASE_URL}/api/diff",
                             params={"base": base["id"], "compare": rr["id"]},
                             headers=_h(tokens["analyst"]), timeout=30).json()
            assert d["regressed"] > 0, f"expected regressions with stricter policy: {d}"
            assert d["total_changes"] > 0
            # verify change payload shape
            c = d["changes"][0]
            for k in ("key", "kind", "base_state", "compare_state"):
                assert k in c
        finally:
            # 5. RESTORE policy to default
            restore = requests.post(f"{BASE_URL}/api/policies",
                                    headers=_h(tokens["admin"]),
                                    json={"amount_tolerance_paise": 100,
                                          "timing_lag_days": 1,
                                          "auto_post_confidence": 0.95,
                                          "note": "restore default after TEST"},
                                    timeout=15)
            assert restore.status_code in (200, 201), f"failed to restore policy: {restore.text}"

    def test_diff_unknown_batch(self, tokens):
        r = requests.get(f"{BASE_URL}/api/diff",
                         params={"base": "nope", "compare": "nada"},
                         headers=_h(tokens["analyst"]), timeout=15)
        assert r.status_code == 404
