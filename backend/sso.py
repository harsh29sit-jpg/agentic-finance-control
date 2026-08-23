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


import secrets as _stdlib_secrets

def _sign(payload_b64):
    return hmac.new(_hmac_key(), payload_b64.encode(), hashlib.sha256).hexdigest()


def make_flow_bundle():
    """Signed cookie bundle: anti-CSRF state + PKCE verifier + nonce."""
    state = _stdlib_secrets.token_urlsafe(16)
    verifier = _stdlib_secrets.token_urlsafe(48)
    nonce = _stdlib_secrets.token_urlsafe(12)
    blob = base64.urlsafe_b64encode(
        json.dumps({"state": state, "verifier": verifier,
                    "nonce": nonce}).encode()).decode().rstrip("=")
    return {"state": state, "verifier": verifier, "nonce": nonce}, \
        f"{blob}.{_sign(blob)}"


def open_bundle(cookie_value):
    """Returns dict or None. Constant-time signature check."""
    try:
        blob, sig = cookie_value.rsplit(".", 1)
        if not hmac.compare_digest(_sign(blob), sig):
            return None
        pad = "=" * (-len(blob) % 4)
        return json.loads(base64.urlsafe_b64decode(blob + pad))
    except Exception:  # noqa: BLE001
        return None


def pkce_challenge(verifier):
    return base64.urlsafe_b64encode(
        hashlib.sha256(verifier.encode()).digest()).decode().rstrip("=")


# legacy single-token state kept for compat checks
def make_state():
    raw = _stdlib_secrets.token_urlsafe(16)
    sig = _sign(raw)
    return f"{raw}.{sig}"


def verify_state(state):
    try:
        raw, sig = state.split(".", 1)
        return hmac.compare_digest(_sign(raw), sig)
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


def authorize_url(discovery, state, code_challenge=None, nonce=None):
    params = {
        "response_type": "code",
        "client_id": os.environ["OIDC_CLIENT_ID"],
        "redirect_uri": f"{os.environ['PUBLIC_BASE_URL'].rstrip('/')}/api/auth/sso/callback",
        "scope": "openid email profile",
        "state": state,
    }
    if code_challenge:
        params["code_challenge"] = code_challenge
        params["code_challenge_method"] = "S256"
    if nonce:
        params["nonce"] = nonce
    sep = "&" if "?" in discovery["authorization_endpoint"] else "?"
    return discovery["authorization_endpoint"] + sep + urllib.parse.urlencode(params)


def exchange_code(discovery, code, code_verifier=None):
    form = {
        "grant_type": "authorization_code",
        "code": code,
        "redirect_uri": f"{os.environ['PUBLIC_BASE_URL'].rstrip('/')}/api/auth/sso/callback",
        "client_id": os.environ["OIDC_CLIENT_ID"],
        "client_secret": os.environ["OIDC_CLIENT_SECRET"],
    }
    if code_verifier:
        form["code_verifier"] = code_verifier
    return http_post_form(discovery["token_endpoint"], form)


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


# ------------------------------------------------------------------ id_token
def _b64url_decode(part):
    return base64.urlsafe_b64decode(part + "=" * (-len(part) % 4))


def verify_id_token(id_token, discovery, nonce=None, client_id=None):
    """RS256 signature check against the issuer's JWKS plus iss/aud/exp/nonce.

    Returns verified claims. Raises ValueError on any failure — callers treat
    SSO login as failed rather than trusting unverified tokens.
    """
    import time as time_mod
    from cryptography.exceptions import InvalidSignature
    from cryptography.hazmat.primitives import hashes
    from cryptography.hazmat.primitives.asymmetric import rsa as rsa_mod
    from cryptography.hazmat.primitives.asymmetric import padding

    header_b64, payload_b64, sig_b64 = id_token.split(".")
    header = json.loads(_b64url_decode(header_b64))
    claims = json.loads(_b64url_decode(payload_b64))

    if header.get("alg") != "RS256":
        raise ValueError(f"unsupported id_token alg {header.get('alg')!r}")

    jwks = http_get_json(discovery["jwks_uri"])
    key = next((k for k in jwks.get("keys", [])
                if k.get("kid") == header.get("kid")), None)
    if not key:
        raise ValueError("id_token kid not present in JWKS")

    n = int.from_bytes(_b64url_decode(key["n"]), "big")
    e = int.from_bytes(_b64url_decode(key["e"]), "big")
    pub = rsa_mod.RSAPublicNumbers(e, n).public_key()
    try:
        pub.verify(_b64url_decode(sig_b64),
                   f"{header_b64}.{payload_b64}".encode(),
                   padding.PKCS1v15(), hashes.SHA256())
    except InvalidSignature:
        raise ValueError("id_token signature invalid")

    now = int(time_mod.time())
    if claims.get("iss") != discovery.get("issuer"):
        raise ValueError("id_token iss mismatch")
    aud = os.environ["OIDC_CLIENT_ID"] if client_id is None else client_id
    if claims.get("aud") not in (aud, [aud]):
        raise ValueError("id_token aud mismatch")
    if claims.get("exp", 0) < now - 60:
        raise ValueError("id_token expired")
    if nonce and claims.get("nonce") and claims["nonce"] != nonce:
        raise ValueError("id_token nonce mismatch")
    return claims
