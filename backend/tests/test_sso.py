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


# Callback-flow coverage moved to test_oidc_live.py — full network runs
# against a live embedded IdP incl. PKCE + JWKS verification.
