"""Gap-1/2/3 hardening: Razorpay API client, realistic Paysim pipeline,
bounded recovery orchestrator. Runs against the embedded per-worker API."""
import os
import time

import pytest
import requests

from connectors import razorpay_api as rzapi

BASE_URL = os.environ["REACT_APP_BACKEND_URL"].rstrip("/")

USERS = {"analyst": ("analyst@recon.io", "analyst123"),
         "controller": ("controller@recon.io", "controller123"),
         "support": ("support@recon.io", "support123")}


def _login(role):
    email, pw = USERS[role]
    r = requests.post(f"{BASE_URL}/api/auth/login",
                      json={"email": email, "password": pw}, timeout=30)
    return {"Authorization": f"Bearer {r.json()['token']}"}


# ---------------------------------------------------------------- Gap 1: API client
class TestRazorpayApiClient:
    def _fake_transport(self, settlements_pages, payments_pages):
        calls = {"n": 0}

        def transport(method, url):
            if "/settlements" in url:
                pages = settlements_pages
            elif "/payments" in url:
                pages = payments_pages
            else:
                raise AssertionError(f"unexpected path {url}")
            idx = min(calls["n"] // 1, len(pages) - 1) if pages else 0
            page = pages[min(calls["n"], len(pages) - 1)]
            calls["n"] += 1
            return 200, {"items": page}
        return transport, calls

    def test_key_validation_rejects_garbage(self):
        with pytest.raises(ValueError):
            rzapi.validate_keys("sk_live_nope", "secret")
        with pytest.raises(ValueError):
            rzapi.validate_keys("rzp_test_123", "")
        rzapi.validate_keys("rzp_test_abc", "longenough")  # no raise

    def test_fetch_settlements_filters_and_maps_paise(self):
        pages = [[
            {"id": "setl_A1", "status": "processed", "utr": " hdfc123 ",
             "amount": 998000, "created_at": 1767225600},
            {"id": "setl_PEND", "status": "pending", "amount": 5,
             "created_at": 1767225600},          # skipped
            {"id": "setl_NOUTR", "status": "processed", "amount": 7,
             "created_at": 1767225600},          # skipped (no utr)
        ]]
        t, _ = self._fake_transport(pages, [])
        rows = rzapi.fetch_settlements("rzp_test_x", "secret12345",
                                       transport=t)
        assert [r["external_id"] for r in rows] == ["setl_A1"]
        assert rows[0]["source"] == "B" and rows[0]["amount"] == 998000
        assert rows[0]["utr"] == "HDFC123"

    def test_fetch_payments_requires_settlement_link(self):
        pages = [[
            {"id": "pay_OK", "status": "captured", "settlement_id": "setl_A1",
             "amount": 250000, "created_at": 1767225600},
            {"id": "pay_NOSETL", "status": "captured", "amount": 9},
            {"id": "pay_FAIL", "status": "failed", "settlement_id": "setl_A1"},
        ]]
        t, _ = self._fake_transport([], pages)
        rows = rzapi.fetch_payments("rzp_test_x", "secret12345", transport=t)
        assert [r["external_id"] for r in rows] == ["pay_OK"]
        assert rows[0]["source"] == "A" and rows[0]["settlement_id"] == "SETL_A1"

    def test_pagination_walks_all_pages(self):
        page = lambda skip: [{"id": f"setl_{skip}_{i}", "status": "processed",
                              "utr": f"U{skip}{i}", "amount": 100 + i,
                              "created_at": 1767225600} for i in range(100)]
        seen_paths = []

        def t(method, url):
            seen_paths.append(url.split("|AUTH|")[0])
            skip = 100 if len(seen_paths) > 1 else 0
            items = [] if len(seen_paths) > 2 else page(skip)
            return 200, {"items": items}
        rows = rzapi.fetch_settlements("rzp_test_x", "secret12345", transport=t)
        assert len(rows) == 200
        assert sum(1 for p in seen_paths if "skip=100" in p) == 1

    def test_derive_bank_rows_identity_and_miss(self):
        b = [{"source": "B", "external_id": "s1", "settlement_id": "S1",
              "utr": "UTR1", "amount": 500, "txn_date": "2026-01-01"}]
        ident, missed0 = rzapi.derive_bank_rows(b, miss_rate=0.0)
        assert len(ident) == 1 and missed0 == 0 and ident[0]["source"] == "C"
        misses = {rzapi.derive_bank_rows(b * 50, miss_rate=0.5, seed=s)[1]
                  for s in range(6)}
        assert any(m > 0 for m in misses)

    def test_sync_endpoint_deduplicates_and_maps_sources(self):
        """Full pull through vaulted creds via injected transport override."""
        old = rzapi._transport_override
        settle = {"id": "setl_SYNC1", "status": "processed", "utr": "UTRSYNC1",
                  "amount": 100000, "created_at": int(time.time())}
        pays = {"id": "pay_S1", "status": "captured",
                "settlement_id": "setl_SYNC1", "amount": 102000,
                "created_at": int(time.time())}

        def fake(method, url):
            return 200, {"items": [settle if "/settlements" in url else pays]}
        rzapi._transport_override = fake
        try:
            # seed credentials into the vault (controller-only route)
            requests.put(f"{BASE_URL}/api/integrations/razorpay/credentials",
                         headers=_login("controller"),
                         json={"key_id": "rzp_test_sync", "key_secret":
                               "syncsecret1"}, timeout=30)
            r = requests.post(f"{BASE_URL}/api/integrations/razorpay/sync",
                              headers=_login("analyst"), json={"hours_back": 24},
                              timeout=120)
            assert r.status_code == 200, r.text
            body = r.json()
            if not body.get("deduplicated"):
                counts = body["counts"]
                assert counts["A"] >= 1 and counts["B"] == 1 and counts["C"] == 1
                label = body["source_label"]
                assert label.startswith("connector:razorpay-api(A:")
            else:
                assert "id" in body
            # second identical sync must deduplicate
            r2 = requests.post(f"{BASE_URL}/api/integrations/razorpay/sync",
                               headers=_login("analyst"), json={"hours_back": 24},
                               timeout=120)
            assert r2.json().get("deduplicated") is True
        finally:
            rzapi._transport_override = old


# ---------------------------------------------------------------- Gap 2: realistic
class TestRealisticPipeline:
    def test_run_realistic_end_to_end_with_benchmark(self):
        h = _login("analyst")
        r = requests.post(f"{BASE_URL}/api/batches/run-realistic", headers=h,
                          timeout=300)
        assert r.status_code == 200, r.text
        body = r.json()
        prof = body["profile"]
        bench = body          # score merged at top level
        assert prof["settlements"] >= 500
        for k in ("timing_lag", "amount_drift", "duplicates", "missing_in_bank",
                  "unidentified"):
            assert prof[k] > 0, f"anomaly class {k} absent"
        assert bench["has_truth"] is True
        assert bench["auto_match_precision"] >= 99.0
        assert bench["match_recall"] >= 99.0
        assert bench["exception_recall"] == 100.0
        assert bench["false_match_rate"] < 0.5
        assert all(bench["gates"].values())

    def test_support_cannot_run_realistic(self):
        r = requests.post(f"{BASE_URL}/api/batches/run-realistic",
                          headers=_login("support"), timeout=30)
        assert r.status_code == 403


# ---------------------------------------------------------------- Gap 3: recovery
class TestRecoveryOrchestrator:
    @pytest.fixture(scope="class")
    def batch(self):
        r = requests.post(f"{BASE_URL}/api/batches/run-realistic",
                          headers=_login("analyst"), timeout=300)
        assert r.status_code == 200
        return r.json()["batch"]

    def _open_cases(self, batch_id, taxonomy=None):
        q = f"batch_id={batch_id}" + (f"&taxonomy={taxonomy}" if taxonomy else "")
        r = requests.get(f"{BASE_URL}/api/exceptions?{q}", headers=_login("analyst"),
                         timeout=30)
        return r.json()["items"]

    def test_plan_ranks_and_evidences(self, batch):
        r = requests.get(f"{BASE_URL}/api/recovery/plan?batch_id={batch['id']}",
                         headers=_login("analyst"), timeout=60)
        assert r.status_code == 200
        body = r.json()
        assert isinstance(body["plan"], list)
        for item in body["plan"]:
            assert item["action"] in ("link_orphan_credit", "resolve_as_fee",
                                      "draft_bank_query", "sla_watch_auto_clear")
            if item["action"] == "link_orphan_credit":
                assert item["evidence"]["match_rule"]

    def test_execute_recovers_orphans_with_unique_links(self, batch):
        plan = requests.get(
            f"{BASE_URL}/api/recovery/plan?batch_id={batch['id']}",
            headers=_login("analyst"), timeout=60).json()
        orphan_ids = [p["case_id"] for p in plan["plan"]
                      if p["action"] == "link_orphan_credit"]
        assert orphan_ids, "no actionable orphans — profile drifted"
        # Paysim-scale values dwarf the default ₹5L/day cap; raise it so the
        # linkage action itself is exercised (cap behaviour has its own test).
        requests.put(f"{BASE_URL}/api/recovery/policy",
                     headers=_login("controller"),
                     json={"daily_value_cap_paise": 10_000_000_00,
                           "cool_off_hours": 0}, timeout=30)
        try:
            # Paysim values are material -> run through the CHECKER (controller)
            r = requests.post(f"{BASE_URL}/api/recovery/execute",
                              headers=_login("controller"),
                              json={"case_ids": orphan_ids[:5]}, timeout=120)
            assert r.status_code == 200
            results = r.json()["results"]
            outcomes = [x["outcome"] for x in results]
            assert "recovered" in outcomes, results
            m = requests.get(f"{BASE_URL}/api/recovery/metrics",
                             headers=_login("analyst"), timeout=30).json()
            assert m["value_recovered_paise"] > 0
            assert m["attempts"] >= len(orphan_ids[:5])
        finally:
            requests.put(f"{BASE_URL}/api/recovery/policy",
                         headers=_login("controller"),
                         json={"daily_value_cap_paise": 500_000_00,
                               "cool_off_hours": 24}, timeout=30)
            # checker sign-off path for whatever landed in pending_approval
            pend = requests.get(f"{BASE_URL}/api/review/pending",
                                headers=_login("controller"), timeout=30).json()

    def test_material_value_routes_to_pending_approval(self, batch):
        cases = sorted(self._open_cases(batch["id"]),
                       key=lambda c: -c["value_at_risk_paise"])
        material = next((c for c in cases
                         if c["taxonomy"] == "AMOUNT_MISMATCH"
                         and c["value_at_risk_paise"] > 200000), None)
        if not material:
            pytest.skip("no material mismatch in this seeded run")
        r = requests.post(f"{BASE_URL}/api/recovery/execute",
                          headers=_login("analyst"),
                          json={"case_ids": [material["id"]]}, timeout=60)
        res = r.json()["results"][0]
        assert res["outcome"] == "pending_approval"
        assert res["detail"]["rule"] == "pending_approval"

    def test_policy_kill_switch_blocks_everything(self, batch):
        requests.put(f"{BASE_URL}/api/recovery/policy",
                     headers=_login("controller"), json={"enabled": False},
                     timeout=30)
        try:
            cases = self._open_cases(batch["id"], taxonomy="MISSING_IN_BANK")
            target = next((c for c in cases if c["value_at_risk_paise"] <= 200000),
                          None)
            if not target:
                pytest.skip("no small missing-in-bank case available")
            r = requests.post(f"{BASE_URL}/api/recovery/execute",
                              headers=_login("analyst"),
                              json={"case_ids": [target["id"]]}, timeout=60)
            res = r.json()["results"][0]
            assert res["outcome"] == "blocked"
            assert res["detail"]["rule"] == "kill_switch"
        finally:
            requests.put(f"{BASE_URL}/api/recovery/policy",
                         headers=_login("controller"), json={"enabled": True},
                         timeout=30)

    def test_daily_value_cap_enforced(self, batch):
        requests.put(f"{BASE_URL}/api/recovery/policy",
                     headers=_login("controller"),
                     json={"daily_value_cap_paise": 1, "cool_off_hours": 0},
                     timeout=30)
        try:
            cases = self._open_cases(batch["id"], taxonomy="MISSING_IN_BANK")
            target = next((c for c in cases if c["value_at_risk_paise"] <= 200000),
                          None)
            if not target:
                pytest.skip("no eligible case under checker threshold")
            r = requests.post(f"{BASE_URL}/api/recovery/execute",
                              headers=_login("analyst"),
                              json={"case_ids": [target["id"]]}, timeout=60)
            res = r.json()["results"][0]
            assert res["outcome"] == "blocked"
            assert res["detail"]["rule"] == "daily_value_cap"
        finally:
            requests.put(f"{BASE_URL}/api/recovery/policy",
                         headers=_login("controller"),
                         json={"daily_value_cap_paise": 500_000_00,
                               "cool_off_hours": 24}, timeout=30)

    def test_policy_route_is_controller_only(self, batch):
        r = requests.put(f"{BASE_URL}/api/recovery/policy",
                         headers=_login("analyst"), json={"enabled": True},
                         timeout=30)
        assert r.status_code == 403
