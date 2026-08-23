"""OIDC SSO: config gating, state integrity, callback provisioning.

IdP HTTP interactions are monkeypatched (in-process server) so the full
redirect -> callback -> user-provisioning path runs without a live IdP.
"""
import os

import pytest
import requests

BASE_URL = os.environ["REACT_APP_BACKEND_URL"].rstrip("/")


@pytest.fixture
def fake_idp(monkeypatch):
    """Pretend an OIDC issuer is configured and reachable."""
    import sso as sso_mod

    monkeypatch.setenv("OIDC_ISSUER_URL", "https://idp.example.com")
    monkeypatch.setenv("OIDC_CLIENT_ID", "recon-control")
    monkeypatch.setenv("OIDC_CLIENT_SECRET", "shhh")
    monkeypatch.setenv("PUBLIC_BASE_URL", "https://control.example.com")

    discovery = {
        "authorization_endpoint": "https://idp.example.com/authorize",
        "token_endpoint": "https://idp.example.com/token",
        "userinfo_endpoint": "https://idp.example.com/userinfo",
    }
    monkeypatch.setattr(sso_mod, "_discovery", lambda: discovery)
    monkeypatch.setattr(sso_mod, "http_get_json", lambda url: discovery)
    monkeypatch.setattr(
        sso_mod, "exchange_code",
        lambda disc, code: {"access_token": f"at-{code}",
                            "id_token": "fake-jwt"})

    holder = {"email": "newbie@corp.example.com"}

    def userinfo(disc, access_token):
        return {"email": holder["email"], "name": "SSO Newbie"}

    monkeypatch.setattr(sso_mod, "fetch_email",
                        lambda disc, tok: userinfo(disc, tok)["email"])
    return holder


class TestSsoConfig:
    def test_disabled_without_env(self, monkeypatch):
        for k in ("OIDC_ISSUER_URL", "OIDC_CLIENT_ID",
                  "OIDC_CLIENT_SECRET", "PUBLIC_BASE_URL"):
            monkeypatch.delenv(k, raising=False)
        r = requests.get(f"{BASE_URL}/api/auth/sso/config", timeout=30)
        assert r.json() == {"enabled": False}

    def test_login_501_when_unconfigured(self, monkeypatch):
        for k in ("OIDC_ISSUER_URL",):
            monkeypatch.delenv(k, raising=False)
        r = requests.get(f"{BASE_URL}/api/auth/sso/login",
                         allow_redirects=False, timeout=30)
        assert r.status_code == 501


class TestSsoCallbackFlow:
    def test_state_cookie_required(self, fake_idp):
        r = requests.get(f"{BASE_URL}/api/auth/sso/callback",
                         params={"code": "x", "state": "y"},
                         allow_redirects=False, timeout=30)
        assert r.status_code == 400

    def test_full_flow_provisions_analyst_and_logs_in(self, fake_idp):
        s = requests.Session()
        # /login validates env, signs state, redirects to the IdP authorize URL
        r = s.get(f"{BASE_URL}/api/auth/sso/login", allow_redirects=False,
                  timeout=30)
        assert r.status_code == 302
        assert "idp.example.com/authorize" in r.headers["Location"]
        assert "sso_state" in s.cookies

        from urllib.parse import urlparse, parse_qs
        qs = parse_qs(urlparse(r.headers["Location"]).query)
        cb = s.get(f"{BASE_URL}/api/auth/sso/callback",
                   params={"code": "auth-code-1", "state": qs["state"][0]},
                   timeout=30)
        assert cb.status_code == 200, cb.text
        body = cb.json()
        assert body["role"] == "analyst"                      # default provisioned role
        assert body["refresh_token"]                          # same session chain as passwords

        me = requests.get(f"{BASE_URL}/api/auth/me",
                          headers={"Authorization": f"Bearer {body['token']}"},
                          timeout=30)
        assert me.json()["email"] == "newbie@corp.example.com"

    def test_admin_email_list_grants_role(self, fake_idp, monkeypatch):
        email = f"boss-{os.urandom(3).hex()}@corp.example.com"
        fake_idp["email"] = email
        monkeypatch.setenv("SSO_ADMIN_EMAILS", email)

        s = requests.Session()
        r = s.get(f"{BASE_URL}/api/auth/sso/login", allow_redirects=False,
                  timeout=30)
        qs = __import__("urllib.parse", fromlist=["parse_qs"])
        from urllib.parse import urlparse, parse_qs
        q = parse_qs(urlparse(r.headers["Location"]).query)
        cb = s.get(f"{BASE_URL}/api/auth/sso/callback",
                   params={"code": "c2", "state": q["state"][0]}, timeout=30)
        assert cb.status_code == 200
        assert cb.json()["role"] == "admin"

    def test_tampered_state_rejected(self, fake_idp):
        s = requests.Session()
        s.get(f"{BASE_URL}/api/auth/sso/login", allow_redirects=False,
              timeout=30)
        evil = "deadbeef." + "0" * 64
        r = s.get(f"{BASE_URL}/api/auth/sso/callback",
                  params={"code": "c", "state": evil},
                  allow_redirects=False, timeout=30)
        assert r.status_code == 400
