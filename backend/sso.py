"""Generic OIDC (Authorization Code) SSO support.

Enabled only when ALL of these are configured:
  OIDC_ISSUER_URL, OIDC_CLIENT_ID, OIDC_CLIENT_SECRET, PUBLIC_BASE_URL

Flow: /api/auth/sso/login -> IdP (state cookie) -> IdP -> /api/auth/sso/callback
-> code+state validated -> tokens exchanged -> userinfo email mapped to a
local user (auto-provisioned analyst unless listed in SSO_ADMIN_EMAILS)
-> session pair issued exactly like password login (same refresh chain).
"""
import base64
import hashlib
import hmac
import json
import os
import secrets
import urllib.parse
import urllib.request
from datetime import datetime, timezone

STATE_TTL_S = 600


def _hmac_key():
    return hashlib.sha256((os.environ["JWT_SECRET"] + "::sso").encode()).digest()


def make_state():
    raw = secrets.token_urlsafe(16)
    sig = hmac.new(_hmac_key(), raw.encode(), hashlib.sha256).hexdigest()
    return f"{raw}.{sig}"


def verify_state(state):
    try:
        raw, sig = state.split(".", 1)
        expect = hmac.new(_hmac_key(), raw.encode(), hashlib.sha256).hexdigest()
        return hmac.compare_digest(sig, expect)
    except Exception:  # noqa: BLE001
        return False


def http_get_json(url):
    req = urllib.request.Request(url, headers={"Accept": "application/json"})
    with urllib.request.urlopen(req, timeout=10) as resp:  # noqa: S310
        return json.loads(resp.read())


def http_post_form(url, form):
    data = urllib.parse.urlencode(form).encode()
    req = urllib.request.Request(
        url, data=data,
        headers={"Content-Type": "application/x-www-form-urlencoded",
                 "Accept": "application/json"})
    with urllib.request.urlopen(req, timeout=10) as resp:  # noqa: S310
        return json.loads(resp.read())


def sso_enabled():
    return all(os.environ.get(k) for k in
               ("OIDC_ISSUER_URL", "OIDC_CLIENT_ID", "OIDC_CLIENT_SECRET",
                "PUBLIC_BASE_URL"))


def _discovery():
    issuer = os.environ["OIDC_ISSUER_URL"].rstrip("/")
    return http_get_json(f"{issuer}/.well-known/openid-configuration")


def authorize_url(discovery, state):
    params = {
        "response_type": "code",
        "client_id": os.environ["OIDC_CLIENT_ID"],
        "redirect_uri": f"{os.environ['PUBLIC_BASE_URL'].rstrip('/')}/api/auth/sso/callback",
        "scope": "openid email profile",
        "state": state,
    }
    sep = "&" if "?" in discovery["authorization_endpoint"] else "?"
    return discovery["authorization_endpoint"] + sep + urllib.parse.urlencode(params)


def exchange_code(discovery, code):
    return http_post_form(discovery["token_endpoint"], {
        "grant_type": "authorization_code",
        "code": code,
        "redirect_uri": f"{os.environ['PUBLIC_BASE_URL'].rstrip('/')}/api/auth/sso/callback",
        "client_id": os.environ["OIDC_CLIENT_ID"],
        "client_secret": os.environ["OIDC_CLIENT_SECRET"],
    })


def fetch_email(discovery, access_token):
    info = http_get_json(
        discovery["userinfo_endpoint"] +
        ("&" if "?" in discovery["userinfo_endpoint"] else "?") +
        urllib.parse.urlencode({}))
    req = urllib.request.Request(  # noqa: S310 — userinfo requires auth header
        discovery["userinfo_endpoint"],
        headers={"Authorization": f"Bearer {access_token}",
                 "Accept": "application/json"})
    with urllib.request.urlopen(req, timeout=10) as resp:
        return json.loads(resp.read()).get("email", "").lower()


async def upsert_sso_user(db, email, name=""):
    """Map an SSO identity to a local user; auto-provision as analyst unless
    the email is listed in SSO_ADMIN_EMAILS (then admin)."""
    from auth import hash_password  # local import avoids cycles
    user = await db.users.find_one({"email": email})
    now = datetime.now(timezone.utc).isoformat()
    admins = {e.strip().lower() for e in
              os.environ.get("SSO_ADMIN_EMAILS", "").split(",") if e.strip()}
    if not user:
        res = await db.users.insert_one({
            "email": email, "name": name or email.split("@")[0],
            "role": role_of(email), "created_at": now,
            # unguessable local password: SSO identities never password-login
            "password_hash": hash_password(os.urandom(24).hex()),
            "sso": True})
        uid = str(res.inserted_id)
        role = role_of(email)
    else:
        uid = str(user["_id"])
        role = user.get("role", "analyst")
        if email in admins and role != "admin":
            role = "admin"
            await db.users.update_one({"_id": user["_id"]},
                                      {"$set": {"role": "admin"}})
    return uid, email, name or (user or {}).get("name", ""), role


def role_of(email):
    admins = {e.strip().lower() for e in
              os.environ.get("SSO_ADMIN_EMAILS", "").split(",") if e.strip()}
    return "admin" if email.lower() in admins else "analyst"
