"""Vault-backed Razorpay credentials + scheduled API sync (transport-injected)."""
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


@pytest.fixture(scope="module")
def admin():
    return _login(ADMIN)


@pytest.fixture(scope="module")
def controller():
    return _login(CONTROLLER)


@pytest.fixture(scope="module")
def analyst():
    return _login(ANALYST)


CANNED_API_RESPONSE = {
    "count": 3,
    "items": [
        {"id": "setl_API001", "status": "processed", "amount": 1234500,
         "utr": "HDFC2699911111111", "created_at": 1782000000},
        {"id": "setl_API002", "status": "processed", "amount": 77700,
         "utr": "ICIC2699922222222", "created_at": 1782086400},
        {"id": "setl_API003", "status": "pending", "amount": 500,
         "utr": "HDFC2699933333333", "created_at": 1782172800},
    ],
}


@pytest.fixture
def fake_transport(monkeypatch):
    """Inject canned upstream responses into the API client."""
    from connectors import razorpay_api as rzapi
    calls = []

    def transport(url):
        calls.append(url)
        return json.loads(json.dumps(CANNED_API_RESPONSE))

    monkeypatch.setattr(rzapi, "_transport_override", transport)
    return calls


class TestCredentialsVault:
    def test_roundtrip_masked_and_never_plaintext(self, admin, controller):
        r = requests.put(f"{BASE_URL}/api/integrations/razorpay/credentials",
                         headers=admin,
                         json={"key_id": "rzp_test_key123456",
                               "key_secret": "SUPERSECRETVALUE"}, timeout=30)
        assert r.status_code == 200
        assert r.json()["key_id_masked"].startswith("rzp_te")

        g = requests.get(f"{BASE_URL}/api/integrations/razorpay/credentials",
                         headers=controller, timeout=30)
        body = g.json()
        assert body["configured"] is True
        assert "SUPERSECRETVALUE" not in json.dumps(body)

        # raw collection must not contain the plaintext secret either
        import pymongo, asyncio
        from motor.motor_asyncio import AsyncIOMotorClient

        async def peek():
            db = AsyncIOMotorClient(os.environ["MONGO_URL"])[os.environ["DB_NAME"]]
            doc = await db.vault_secrets.find_one({"name": "razorpay_api"})
            return doc["blob"] if doc else None

        blob = asyncio.run(peek())
        assert blob and "SUPERSECRETVALUE" not in blob

        d = requests.delete(f"{BASE_URL}/api/integrations/razorpay/credentials",
                            headers=controller, timeout=30)
        assert d.status_code == 200
        g2 = requests.get(f"{BASE_URL}/api/integrations/razorpay/credentials",
                          headers=controller, timeout=30).json()
        assert g2["configured"] is False

    def test_analyst_cannot_write_credentials(self, analyst):
        r = requests.put(f"{BASE_URL}/api/integrations/razorpay/credentials",
                         headers=analyst,
                         json={"key_id": "k", "key_secret": "v" * 8}, timeout=30)
        assert r.status_code == 403


class TestApiSync:
    def test_sync_creates_batch_and_is_idempotent(self, admin, fake_transport):
        requests.delete(f"{BASE_URL}/api/integrations/razorpay/credentials",
                        headers=admin, timeout=30)
        requests.put(f"{BASE_URL}/api/integrations/razorpay/credentials",
                     headers=admin,
                     json={"key_id": "rzp_test_key999999",
                           "key_secret": "sekrit-value-1"}, timeout=30)

        r = requests.post(f"{BASE_URL}/api/integrations/razorpay/sync",
                          headers=admin, json={"hours_back": 24}, timeout=120)
        assert r.status_code == 200, r.text
        b = r.json()
        assert b["counts"]["B"] == 2                       # pending skipped
        # settlements without bank credits on file -> MISSING_IN_BANK per case
        excs = requests.get(
            f"{BASE_URL}/api/exceptions?batch_id={b['id']}&taxonomy=MISSING_IN_BANK",
            headers=admin, timeout=30).json()["items"]
        utrs = {e["utr"] for e in excs}
        assert {"HDFC2699911111111", "ICIC2699922222222"} <= utrs
        amounts_by_utr = {e["utr"]: e["value_at_risk_paise"] for e in excs}
        assert amounts_by_utr["HDFC2699911111111"] == 1234500   # paise verbatim

        r2 = requests.post(f"{BASE_URL}/api/integrations/razorpay/sync",
                           headers=admin, json={"hours_back": 24}, timeout=120)
        assert r2.json().get("deduplicated") is True       # same window -> same batch

    def test_missing_credentials_409(self, admin):
        requests.delete(f"{BASE_URL}/api/integrations/razorpay/credentials",
                        headers=admin, timeout=30)
        r = requests.post(f"{BASE_URL}/api/integrations/razorpay/sync",
                          headers=admin, json={"hours_back": 24}, timeout=60)
        assert r.status_code == 409
        assert "not configured" in r.json()["detail"]

    def test_support_blocked_from_sync(self):
        support = _login(("support@recon.io", "support123"))
        r = requests.post(f"{BASE_URL}/api/integrations/razorpay/sync",
                          headers=support, json={}, timeout=60)
        assert r.status_code == 403


class TestScheduledSync:
    def test_razorpay_sync_schedule_runs_via_run_now(self, admin, fake_transport):
        requests.put(f"{BASE_URL}/api/integrations/razorpay/credentials",
                     headers=admin,
                     json={"key_id": "rzp_test_sched01",
                           "key_secret": "sched-secret-1"}, timeout=30)

        r = requests.post(f"{BASE_URL}/api/schedules", headers=admin,
                          json={"name": "nightly rzp pull",
                                "cron": "15 2 * * *",
                                "action": "razorpay_sync"}, timeout=30)
        assert r.status_code == 200, r.text
        sid = r.json()["id"]

        run = requests.post(f"{BASE_URL}/api/schedules/{sid}/run-now",
                            headers=admin, timeout=180)
        assert run.status_code == 200, run.text
        body = run.json()
        assert body["schedule"]["last_status"] in ("ok", "failed")

        requests.delete(f"{BASE_URL}/api/schedules/{sid}", headers=admin,
                        timeout=30)
