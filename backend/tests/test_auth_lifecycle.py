"""Auth lifecycle: refresh rotation, logout revocation, account lockout,
password change session invalidation."""
import os

import pytest
import requests

BASE_URL = os.environ["REACT_APP_BACKEND_URL"].rstrip("/")


def _login(email, password):
    return requests.post(f"{BASE_URL}/api/auth/login",
                         json={"email": email, "password": password}, timeout=30)


@pytest.fixture
def fresh_user():
    """Register a throwaway analyst (avoids poisoning shared fixtures with lockouts)."""
    email = f"lifecycle_{os.urandom(4).hex()}@test.io"
    r = requests.post(f"{BASE_URL}/api/auth/register",
                      json={"email": email, "password": "lifecycle-pass-1",
                            "name": "Lifecycle Probe", "role": "analyst"}, timeout=30)
    assert r.status_code == 200, r.text
    data = r.json()
    data["password"] = "lifecycle-pass-1"
    return data


def _me(token):
    return requests.get(f"{BASE_URL}/api/auth/me",
                        headers={"Authorization": f"Bearer {token}"}, timeout=30)


class TestRefreshRotation:
    def test_login_issues_refresh_and_access(self, fresh_user):
        assert fresh_user["token"]
        assert len(fresh_user["refresh_token"]) >= 32
        assert _me(fresh_user["token"]).status_code == 200

    def test_refresh_rotates_and_old_token_dies(self, fresh_user):
        old_refresh = fresh_user["refresh_token"]
        r1 = requests.post(f"{BASE_URL}/api/auth/refresh",
                           json={"refresh_token": old_refresh}, timeout=30)
        assert r1.status_code == 200
        new = r1.json()
        assert new["token"] and new["refresh_token"] != old_refresh

        # old refresh is single-use: replay must fail
        r2 = requests.post(f"{BASE_URL}/api/auth/refresh",
                           json={"refresh_token": old_refresh}, timeout=30)
        assert r2.status_code == 401

        # rotated chain continues to work
        r3 = requests.post(f"{BASE_URL}/api/auth/refresh",
                           json={"refresh_token": new["refresh_token"]}, timeout=30)
        assert r3.status_code == 200

    def test_garbage_refresh_rejected(self):
        r = requests.post(f"{BASE_URL}/api/auth/refresh",
                          json={"refresh_token": "totally-made-up"}, timeout=30)
        assert r.status_code == 401

    def test_logout_revokes_chain(self, fresh_user):
        refresh = fresh_user["refresh_token"]
        lo = requests.post(f"{BASE_URL}/api/auth/logout",
                           headers={"Authorization": f"Bearer {fresh_user['token']}"},
                           json={"refresh_token": refresh}, timeout=30)
        assert lo.status_code == 200
        r = requests.post(f"{BASE_URL}/api/auth/refresh",
                          json={"refresh_token": refresh}, timeout=30)
        assert r.status_code == 401


class TestAccountLockout:
    def test_eight_failures_lock_even_correct_password(self, fresh_user):
        email, password = fresh_user["email"], fresh_user["password"]

        for i in range(8):
            bad = _login(email, "wrong-password")
            assert bad.status_code == 401

        locked = _login(email, password)          # correct creds, locked account
        assert locked.status_code == 429
        assert "locked" in locked.json()["detail"].lower()

    def test_successful_login_resets_counter(self, fresh_user):
        email, password = fresh_user["email"], fresh_user["password"]
        for _ in range(3):
            _login(email, "nope")
        ok = _login(email, password)
        assert ok.status_code == 200
        # counter reset: three more failures must NOT lock yet
        for _ in range(3):
            assert _login(email, "nope").status_code == 401
        again = _login(email, password)
        assert again.status_code == 200


class TestPasswordChange:
    def test_change_revokes_sessions_and_requires_relogin(self, fresh_user):
        old_access, old_refresh = fresh_user["token"], fresh_user["refresh_token"]

        # refresh once so we know rotation still worked pre-change
        assert requests.post(f"{BASE_URL}/api/auth/refresh",
                             json={"refresh_token": old_refresh},
                             timeout=30).status_code == 200

        r = requests.post(f"{BASE_URL}/api/auth/change-password",
                          headers={"Authorization": f"Bearer {old_access}"},
                          json={"current": fresh_user["password"],
                                "next": "brand-new-pass-2"}, timeout=30)
        assert r.status_code == 200

        # all sessions revoked: stored refresh no longer usable...
        assert requests.post(f"{BASE_URL}/api/auth/refresh",
                             json={"refresh_token": old_refresh},
                             timeout=30).status_code == 401
        # ...and the OLD password can't log back in
        assert _login(fresh_user["email"], fresh_user["password"]).status_code == 401
        # NEW password works and issues a fresh chain
        relog = _login(fresh_user["email"], "brand-new-pass-2")
        assert relog.status_code == 200
        assert relog.json()["refresh_token"]

    def test_change_rejects_wrong_current(self, fresh_user):
        r = requests.post(f"{BASE_URL}/api/auth/change-password",
                          headers={"Authorization": f"Bearer {fresh_user['token']}"},
                          json={"current": "not-my-password", "next": "whatever-pass"},
                          timeout=30)
        assert r.status_code == 401
