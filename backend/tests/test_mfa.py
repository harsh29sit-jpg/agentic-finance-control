"""TOTP MFA end-to-end: enrol -> enforced login -> recovery codes -> disable."""
import base64
import hashlib
import hmac
import os
import time

import pytest
import requests

from totp import generate_secret, provisioning_uri, verify as totp_verify

BASE_URL = os.environ["REACT_APP_BACKEND_URL"].rstrip("/")
ADMIN = ("admin@recon.io", "admin123")


def _code_at(secret_b32, counter):
    pad = "=" * ((8 - len(secret_b32) % 8) % 8)
    key = base64.b32decode(secret_b32.upper() + pad)
    msg = counter.to_bytes(8, "big")
    d = hmac.new(key, msg, hashlib.sha1).digest()
    o = d[-1] & 0x0F
    v = ((d[o] & 0x7F) << 24) | (d[o+1] << 16) | (d[o+2] << 8) | d[o+3]
    return str(v % 10**6).zfill(6)


def _current_code(secret):
    return _code_at(secret, int(time.time()) // 30)


def _login(email, password, totp=None):
    body = {"email": email, "password": password}
    if totp:
        body["totp"] = totp
    return requests.post(f"{BASE_URL}/api/auth/login", json=body, timeout=30)


@pytest.fixture(scope="module")
def admin():
    r = _login(*ADMIN)
    return {"Authorization": f"Bearer {r.json()['token']}"}


class TestTotpPrimitive:
    def test_verify_accepts_current_code(self):
        s = generate_secret()
        assert totp_verify(s, _current_code(s))

    def test_verify_rejects_wrong_code(self):
        s = generate_secret()
        assert not totp_verify(s, "000000") or _current_code(s) == "000000"

    def test_provisioning_uri_shape(self):
        uri = provisioning_uri("ABC234DEF", "a@b.io", "Recon")
        assert uri.startswith("otpauth://totp/Recon%3Aa%40b.io?")
        assert "secret=ABC234DEF" in uri and "issuer=Recon" in uri


class TestMfaFlow:
    def test_full_lifecycle(self):
        email = f"mfa_{os.urandom(3).hex()}@test.io"
        r = requests.post(f"{BASE_URL}/api/auth/register",
                          json={"email": email, "password": "strong-pass-9",
                                "name": "MFA Probe"}, timeout=30)
        tok = {"Authorization": f"Bearer {r.json()['token']}"}

        # --- setup returns secret + otpauth URI; MFA not yet active
        s = requests.post(f"{BASE_URL}/api/auth/mfa/setup", headers=tok, timeout=30)
        assert s.status_code == 200
        setup = s.json()
        assert setup["secret"] and setup["otpauth_uri"].startswith("otpauth://")

        # --- wrong code rejected at enable
        bad = requests.post(f"{BASE_URL}/api/auth/mfa/enable", headers=tok,
                            json={"code": "000000"}, timeout=30)
        assert bad.status_code == 400

        # --- correct code enables and yields single-use recovery codes
        en = requests.post(f"{BASE_URL}/api/auth/mfa/enable", headers=tok,
                           json={"code": _current_code(setup["secret"])},
                           timeout=30)
        assert en.status_code == 200, en.text
        recovery = en.json()["recovery_codes"]
        assert len(recovery) == 8

        # --- login now demands a code
        no_code = _login(email, "strong-pass-9")
        assert no_code.status_code == 401
        assert no_code.json()["detail"].get("mfa_required") is True

        # --- TOTP code unlocks login
        ok = _login(email, "strong-pass-9", totp=_current_code(setup["secret"]))
        assert ok.status_code == 200, ok.text

        # --- recovery code works exactly once
        rc = recovery[0]
        first = _login(email, "strong-pass-9", totp=rc)
        assert first.status_code == 200
        replay = _login(email, "strong-pass-9", totp=rc)
        assert replay.status_code == 401

        # --- remaining codes still valid
        second = _login(email, "strong-pass-9", totp=recovery[1])
        assert second.status_code == 200

        # --- disable with password restores plain login
        tok2 = {"Authorization":
                f"Bearer {_login(email, 'strong-pass-9', totp=_current_code(setup['secret'])).json()['token']}"}
        dis = requests.post(f"{BASE_URL}/api/auth/mfa/disable", headers=tok2,
                            json={"password": "strong-pass-9"}, timeout=30)
        assert dis.status_code == 200
        plain = _login(email, "strong-pass-9")
        assert plain.status_code == 200
