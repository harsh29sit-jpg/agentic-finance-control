"""Sandbox fixture restructure: tagging, RBAC, dashboard exclusion, rerun inheritance."""
import os
import pytest
import requests

BASE_URL = os.environ["REACT_APP_BACKEND_URL"].rstrip("/")

ADMIN = ("admin@recon.io", "admin123")
CONTROLLER = ("controller@recon.io", "controller123")
ANALYST = ("analyst@recon.io", "analyst123")


def _login(role_creds):
    email, pw = role_creds
    r = requests.post(f"{BASE_URL}/api/auth/login",
                      json={"email": email, "password": pw}, timeout=30)
    return {"Authorization": f"Bearer {r.json()['token']}"}


@pytest.fixture(scope="module")
def controller():
    return _login(CONTROLLER)


@pytest.fixture(scope="module")
def admin():
    return _login(ADMIN)


@pytest.fixture(scope="module")
def analyst():
    return _login(ANALYST)


class TestSandboxRBAC:
    def test_analyst_forbidden_on_sandbox_endpoint(self, analyst):
        r = requests.post(f"{BASE_URL}/api/sandbox/batch", headers=analyst, timeout=60)
        assert r.status_code == 403

    def test_controller_creates_tagged_fixture(self, controller):
        r = requests.post(f"{BASE_URL}/api/sandbox/batch", headers=controller, timeout=120)
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["sandbox"] is True
        assert body["source_label"].startswith("sandbox:")
        assert body["has_truth"] is True          # fixtures carry evaluation labels

    def test_deprecated_alias_still_works_and_tags(self, analyst):
        r = requests.post(f"{BASE_URL}/api/batches/run-demo", headers=analyst, timeout=120)
        assert r.status_code == 200
        assert r.json()["sandbox"] is True

    def test_rerun_inherits_sandbox_flag(self, controller):
        base = requests.post(f"{BASE_URL}/api/sandbox/batch", headers=controller,
                             timeout=120).json()
        rerun = requests.post(f"{BASE_URL}/api/batches/{base['id']}/rerun",
                              headers=controller, timeout=120).json()
        assert rerun["sandbox"] is True


class TestDashboardExclusion:
    def test_sandbox_excluded_by_default_optin_included(self, admin):
        m_default = requests.get(f"{BASE_URL}/api/dashboard/metrics",
                                 headers=admin, timeout=30).json()
        created = requests.post(f"{BASE_URL}/api/sandbox/batch", headers=admin,
                                timeout=120).json()
        m_after = requests.get(f"{BASE_URL}/api/dashboard/metrics",
                               headers=admin, timeout=30).json()
        m_with = requests.get(f"{BASE_URL}/api/dashboard/metrics?include_sandbox=true",
                              headers=admin, timeout=30).json()
        # production view unchanged by the fixture; opt-in sees it
        assert m_after["total_batches"] == m_default["total_batches"]
        assert m_with["total_batches"] > m_default["total_batches"]
