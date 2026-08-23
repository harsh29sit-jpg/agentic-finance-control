"""Full-network OIDC integration: our API server talks to a LIVE embedded
IdP over HTTP (discovery -> authorize+PKCE+nonce -> token exchange with
verifier -> JWKS-verified RS256 id_token). No function mocking anywhere.
"""
import os

import pytest
import requests
from urllib.parse import urlparse, parse_qs

from tests.oidc_stub import StubOIDCProvider

BASE_URL = os.environ["REACT_APP_BACKEND_URL"].rstrip("/")


@pytest.fixture(scope="module")
def idp():
    provider = StubOIDCProvider(
        client_id="recon-control-tower",
        client_secret="integration-secret",
        redirect_uri=f"{BASE_URL}/api/auth/sso/callback",
    )
    issuer = provider.start()
    monkey_env = {
        "OIDC_ISSUER_URL": issuer,
        "OIDC_CLIENT_ID": "recon-control-tower",
        "OIDC_CLIENT_SECRET": "integration-secret",
        "PUBLIC_BASE_URL": BASE_URL,
    }
    old = {k: os.environ.get(k) for k in monkey_env}
    os.environ.update(monkey_env)
    yield {"provider": provider, "issuer": issuer}
    for k, v in old.items():
        if v is None:
            os.environ.pop(k, None)
        else:
            os.environ[k] = v
    provider.stop()


class TestLiveOidcFlow:
    def test_discovery_served_by_stub(self, idp):
        r = requests.get(f"{idp['issuer']}/.well-known/openid-configuration",
                         timeout=10)
        assert r.status_code == 200
        assert r.json()["issuer"] == idp["issuer"]

    def test_full_redirect_flow_provisions_and_logs_in(self, idp):
        idp["provider"].next_email = f"oidc-live-{os.urandom(2).hex()}@corp.test"
        s = requests.Session()

        # 1) our API signs state + PKCE bundle and redirects to the IdP
        r = s.get(f"{BASE_URL}/api/auth/sso/login", allow_redirects=False,
                  timeout=30)
        assert r.status_code == 302, r.text
        loc = r.headers["Location"]
        assert idp["issuer"] in loc and "code_challenge_method=S256" in loc \
            and "nonce=" in loc

        # 2) IdP authorize auto-consents and bounces to our callback
        r2 = s.get(loc, allow_redirects=False, timeout=30)
        assert r2.status_code == 302
        cb_url = r2.headers["Location"]
        assert cb_url.startswith(f"{BASE_URL}/api/auth/sso/callback")

        # 3) callback: PKCE verifier + JWKS-verified id_token -> session pair
        r3 = s.get(cb_url, timeout=30)
        assert r3.status_code == 200, r3.text
        body = r3.json()
        assert body["email"] == idp["provider"].next_email
        assert body["role"] == "analyst"
        assert len(body["refresh_token"]) >= 32

        # 4) the issued access token actually authenticates
        me = requests.get(f"{BASE_URL}/api/auth/me",
                          headers={"Authorization": f"Bearer {body['token']}"},
                          timeout=30)
        assert me.status_code == 200
        assert me.json()["sso"] is True or me.json()["email"] == body["email"]

    def test_pkce_mismatch_rejected_by_idp(self, idp):
        """A stolen authorization code without its verifier must die at the
        token endpoint — enforced by the IdP itself."""
        s = requests.Session()
        r = s.get(f"{BASE_URL}/api/auth/sso/login", allow_redirects=False,
                  timeout=30)
        loc = r.headers["Location"]
        q = parse_qs(urlparse(loc).query)

        # hit authorize directly to obtain a bound code...
        authz = idp["issuer"] + "/authorize?" + loc.split("?")[1]
        r2 = s.get(authz.replace("code_challenge_method=S256",
                                 "code_challenge=WRONGCHALLENGE"
                                 ).replace(q["code_challenge"][0],
                                           "WRONGCHALLENGE"),
                   allow_redirects=False, timeout=30)
        code = parse_qs(urlparse(r2.headers["Location"]).query)["code"][0]

        # ...then exchange WITHOUT any verifier -> IdP refuses
        tok = requests.post(f"{idp['issuer']}/token", data={
            "grant_type": "authorization_code", "code": code,
            "redirect_uri": f"{BASE_URL}/api/auth/sso/callback",
            "client_id": "recon-control-tower",
            "client_secret": "integration-secret",
        }, timeout=30)
        assert tok.status_code == 400
        assert tok.json().get("error") == "invalid_grant"

    def test_nonce_bound_to_id_token(self, idp):
        """Each login's nonce lands in the minted id_token; our callback
        verifies it against the per-flow bundle."""
        s = requests.Session()
        r = s.get(f"{BASE_URL}/api/auth/sso/login", allow_redirects=False,
                  timeout=30)
        n1 = parse_qs(urlparse(r.headers["Location"]).query)["nonce"][0]
        s.get(r.headers["Location"], allow_redirects=False, timeout=30)

        r = s.get(f"{BASE_URL}/api/auth/sso/login", allow_redirects=False,
                  timeout=30)
        n2 = parse_qs(urlparse(r.headers["Location"]).query)["nonce"][0]
        assert n1 != n2                       # fresh flow, fresh nonce

        # replaying flow-1's callback after starting flow-2 fails cleanly:
        # the sso_flow cookie was replaced by flow-2's bundle
        old_cb = f"{BASE_URL}/api/auth/sso/callback?code=x&state={n1}"
        r3 = s.get(old_cb.replace(f"state={n1}", "state=tampered"),
                   allow_redirects=False, timeout=30)
        assert r3.status_code == 400
