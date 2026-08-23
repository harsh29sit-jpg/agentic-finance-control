"""JWT email/password auth + RBAC + refresh lifecycle + TOTP MFA.

Token model:
  - Access token: 30 min, stateless (kept in localStorage by the SPA).
  - Refresh token: 7 days, STATEFUL — only its SHA-256 hash is stored; every
    use rotates it (old one revoked). Compromised refresh tokens are useless
    after first replay and revocable via logout / change-password.
Account lockout:
  - Persistent per-email failed-attempt counters in Mongo: 8 failures in a
    15-minute window locks the account for 15 minutes (429), independent of
    the IP-based rate limiter. Successful login resets the counter.
MFA:
  - RFC 6238 TOTP (stdlib). Pending secret is encrypted at rest (AES-GCM,
    key derived from JWT_SECRET); recovery codes are stored hashed,
    single-use.
"""
import os
import asyncio
import base64
import hashlib
import hmac
import secrets as pysecrets
import jwt
import bcrypt
from datetime import datetime, timezone, timedelta
from fastapi import APIRouter, HTTPException, Request, Response, Depends
from bson import ObjectId

from cryptography.hazmat.primitives.ciphers.aead import AESGCM

import totp as totp_mod
from models import RegisterRequest, LoginRequest

JWT_ALGORITHM = "HS256"
ACCESS_TTL_MIN = 30
REFRESH_TTL_DAYS = 7
LOCKOUT_THRESHOLD = 8
LOCKOUT_WINDOW_S = 900

# Self-service registration is limited to non-privileged roles.
# controller/admin/compliance accounts are provisioned via seeds or by an admin.
SELF_SERVICE_ROLES = ("analyst", "support")

ROLE_LABELS = {
    "admin": "Administrator",
    "controller": "Financial Controller",
    "compliance": "Compliance Officer",
    "analyst": "Reconciliation Analyst",
    "support": "Support (Read-only)",
}


def hash_password(password: str) -> str:
    return bcrypt.hashpw(password.encode("utf-8"), bcrypt.gensalt()).decode("utf-8")


def verify_password(plain: str, hashed: str) -> bool:
    try:
        return bcrypt.checkpw(plain.encode("utf-8"), hashed.encode("utf-8"))
    except (ValueError, TypeError):
        return False


def _secret() -> str:
    return os.environ["JWT_SECRET"]


# ------------------------------------------------------------------ mfa crypto
def _mfa_key() -> bytes:
    return hashlib.sha256((_secret() + "::mfa").encode()).digest()


def _encrypt_secret(plain: str) -> str:
    aes = AESGCM(_mfa_key())
    nonce = pysecrets.token_bytes(12)
    enc = aes.encrypt(nonce, plain.encode(), None)
    return base64.b64encode(nonce + enc).decode()


def _decrypt_secret(blob: str) -> str:
    raw = base64.b64decode(blob)
    aes = AESGCM(_mfa_key())
    return aes.decrypt(raw[:12], raw[12:], None).decode()


RECOVERY_COUNT = 8


def _new_recovery_codes():
    codes = [pysecrets.token_hex(5) for _ in range(RECOVERY_COUNT)]
    hashes = [hashlib.sha256(c.encode()).hexdigest() for c in codes]
    return codes, hashes


def _now():
    return datetime.now(timezone.utc)


def create_access_token(user_id: str, email: str, role: str) -> str:
    payload = {"sub": user_id, "email": email, "role": role,
               "exp": _now() + timedelta(minutes=ACCESS_TTL_MIN),
               "type": "access"}
    return jwt.encode(payload, _secret(), algorithm=JWT_ALGORITHM)


def create_refresh_token() -> tuple[str, str]:
    """Returns (raw_token, token_hash). Only the hash is persisted."""
    raw = os.urandom(32).hex()
    return raw, hashlib.sha256(raw.encode()).hexdigest()


async def _issue_refresh(db, user_id) -> tuple[str, str]:
    raw, th = create_refresh_token()
    await db.refresh_tokens.insert_one({
        "token_hash": th, "user_id": user_id,
        "created_at": _now().isoformat(),
        "expires_at": (_now() + timedelta(days=REFRESH_TTL_DAYS)).isoformat(),
        "revoked": False,
    })
    return raw, th


async def _revoke_refresh(db, token_hash):
    await db.refresh_tokens.update_one({"token_hash": token_hash},
                                       {"$set": {"revoked": True}})


async def _rotate_refresh(db, raw_token):
    """Validate + rotate. Returns user_id or raises 401-mapped errors."""
    th = hashlib.sha256(raw_token.encode()).hexdigest()
    doc = await db.refresh_tokens.find_one({"token_hash": th})
    if not doc or doc.get("revoked"):
        raise HTTPException(status_code=401, detail="Invalid refresh token")
    if doc.get("expires_at") and doc["expires_at"] < _now().isoformat():
        raise HTTPException(status_code=401, detail="Refresh token expired")
    await db.refresh_tokens.update_one({"_id": doc["_id"]},
                                       {"$set": {"revoked": True}})
    new_raw, new_th = create_refresh_token()
    await db.refresh_tokens.insert_one({
        "token_hash": new_th, "user_id": doc["user_id"],
        "created_at": _now().isoformat(),
        "expires_at": (_now() + timedelta(days=REFRESH_TTL_DAYS)).isoformat(),
        "revoked": False, "rotated_from": th,
    })
    return doc["user_id"], new_raw


# ------------------------------------------------------------------ lockout
async def _lockout_state(db, email):
    doc = await db.auth_failures.find_one({"email": email})
    if not doc:
        return 0, None
    locked_until = doc.get("locked_until")
    if locked_until:
        if locked_until > _now().isoformat():
            return doc.get("count", 0), locked_until
    window_start = (_now() - timedelta(seconds=LOCKOUT_WINDOW_S)).isoformat()
    recent = doc.get("updated_at", "") >= window_start
    return (doc.get("count", 0) if recent else 0), None


async def _record_failure(db, email):
    await db.auth_failures.update_one(
        {"email": email},
        {"$inc": {"count": 1}, "$set": {"updated_at": _now().isoformat()}},
        upsert=True)
    count, _ = await _lockout_state(db, email)
    if count >= LOCKOUT_THRESHOLD:
        locked = (_now() + timedelta(seconds=LOCKOUT_WINDOW_S)).isoformat()
        await db.auth_failures.update_one({"email": email},
                                          {"$set": {"locked_until": locked}})


def _cookie_secure():
    return os.environ.get("COOKIE_SECURE", "true").lower() in ("1", "true", "yes")


def _set_cookie(response: Response, token: str):
    response.set_cookie("access_token", token, httponly=True, secure=_cookie_secure(),
                        samesite="none" if _cookie_secure() else "lax",
                        max_age=ACCESS_TTL_MIN * 60, path="/")


def build_auth_router(db, limiter=None):
    router = APIRouter(prefix="/api/auth", tags=["auth"])

    async def _rate_ok(request: Request, key_extra: str) -> bool:
        """Shared, cross-instance rate limit (limiter may be sync or async)."""
        if limiter is None:
            return True
        ip = request.client.host if request.client else "unknown"
        result = limiter.allow(f"{ip}:{key_extra}", max_events=30, window_seconds=60)
        if asyncio.iscoroutine(result):
            result = await result
        return bool(result)

    async def get_current_user(request: Request) -> dict:
        token = request.cookies.get("access_token")
        if not token:
            header = request.headers.get("Authorization", "")
            if header.startswith("Bearer "):
                token = header[7:]
        if not token:
            raise HTTPException(status_code=401, detail="Not authenticated")
        try:
            payload = jwt.decode(token, _secret(), algorithms=[JWT_ALGORITHM])
            if payload.get("type") != "access":
                raise HTTPException(status_code=401, detail="Invalid token type")
            user = await db.users.find_one({"_id": ObjectId(payload["sub"])})
            if not user:
                raise HTTPException(status_code=401, detail="User not found")
            user["id"] = str(user["_id"])
            user.pop("_id", None)
            user.pop("password_hash", None)
            return user
        except jwt.ExpiredSignatureError:
            raise HTTPException(status_code=401, detail="Token expired")
        except jwt.InvalidTokenError:
            raise HTTPException(status_code=401, detail="Invalid token")

    async def _session_payload(user_doc):
        uid = str(user_doc["_id"])
        access = create_access_token(uid, user_doc["email"], user_doc["role"])
        refresh, _th = await _issue_refresh(db, uid)
        return {"id": uid, "email": user_doc["email"], "name": user_doc["name"],
                "role": user_doc["role"], "token": access,
                "refresh_token": refresh}

    @router.post("/register")
    async def register(body: RegisterRequest, request: Request, response: Response):
        if not await _rate_ok(request, "register"):
            raise HTTPException(status_code=429, detail="Too many attempts; retry shortly")
        email = body.email.lower()
        role = body.role if body.role in SELF_SERVICE_ROLES else "analyst"
        if await db.users.find_one({"email": email}):
            raise HTTPException(status_code=400, detail="Email already registered")
        doc = {"email": email, "password_hash": hash_password(body.password),
               "name": body.name, "role": role,
               "created_at": _now().isoformat()}
        res = await db.users.insert_one(doc)
        user_doc = dict(doc)
        user_doc["_id"] = res.inserted_id
        payload = await _session_payload(user_doc)
        _set_cookie(response, payload["token"])
        return payload

    @router.post("/login")
    async def login(body: LoginRequest, request: Request, response: Response):
        if not await _rate_ok(request, body.email.lower()):
            raise HTTPException(status_code=429, detail="Too many login attempts; retry shortly")
        email = body.email.lower()

        count, locked_until = await _lockout_state(db, email)
        if locked_until:
            raise HTTPException(status_code=429,
                                detail="Account temporarily locked after repeated failures; try later")

        user = await db.users.find_one({"email": email})
        if not user or not verify_password(body.password, user["password_hash"]):
            await _record_failure(db, email)
            remaining = max(0, LOCKOUT_THRESHOLD - (count + 1))
            detail = "Invalid email or password"
            if count + 1 >= LOCKOUT_THRESHOLD // 2:
                detail += f" ({remaining} attempts before temporary lock)"
            raise HTTPException(status_code=401, detail=detail)

        # ---- MFA gate (TOTP or single-use recovery code) ----
        if user.get("mfa_enabled"):
            code = (body.totp or "").strip()
            if not code:
                raise HTTPException(
                    status_code=401,
                    detail={"message": "MFA code required", "mfa_required": True})
            secret = _decrypt_secret(user["mfa_secret"])
            if not totp_mod.verify(secret, code):
                rec_hash = hashlib.sha256(code.encode()).hexdigest()
                rec = await db.recovery_codes.find_one_and_update(
                    {"user_id": str(user["_id"]), "hash": rec_hash, "used": False},
                    {"$set": {"used": True, "used_at": _now().isoformat()}})
                if not rec:
                    await _record_failure(db, email)
                    raise HTTPException(status_code=401, detail="Invalid MFA code")

        await db.auth_failures.delete_one({"email": email})
        payload = await _session_payload(user)
        _set_cookie(response, payload["token"])
        return payload

    # ------------------------------------------------------------------ MFA
    @router.post("/mfa/setup")
    async def mfa_setup(request: Request, user: dict = Depends(get_current_user)):
        if user.get("mfa_enabled"):
            raise HTTPException(status_code=400,
                                detail="MFA already enabled; disable first to re-enrol")
        secret = totp_mod.generate_secret()
        enc = _encrypt_secret(secret)
        await db.users.update_one({"_id": ObjectId(user["id"])},
                                  {"$set": {"mfa_secret_enc": enc,
                                            "mfa_enabled": False}})
        uri = totp_mod.provisioning_uri(secret, user["email"], "Recon Control Tower")
        return {"secret": secret, "otpauth_uri": uri}

    @router.post("/mfa/enable")
    async def mfa_enable(request: Request, user: dict = Depends(get_current_user)):
        body = await request.json()
        code = ((body or {}).get("code") or "").strip()
        doc = await db.users.find_one({"_id": ObjectId(user["id"])})
        pending = doc.get("mfa_secret_enc") if doc else None
        if not pending:
            raise HTTPException(status_code=400, detail="Call /mfa/setup first")
        secret = _decrypt_secret(pending)
        if not totp_mod.verify(secret, code):
            raise HTTPException(status_code=400, detail="Invalid code; try the current one")
        codes, hashes = _new_recovery_codes()
        await db.users.update_one({"_id": ObjectId(user["id"])},
                                  {"$set": {"mfa_enabled": True,
                                            "mfa_secret": pending}})
        await db.users.update_one({"_id": ObjectId(user["id"])},
                                  {"$unset": {"mfa_secret_enc": ""}})
        await db.recovery_codes.delete_many({"user_id": user["id"]})
        await db.recovery_codes.insert_many([
            {"user_id": user["id"], "hash": h, "used": False} for h in hashes])
        return {"ok": True, "recovery_codes": codes,
                "note": "store these now; each works exactly once"}

    @router.post("/mfa/disable")
    async def mfa_disable(request: Request, user: dict = Depends(get_current_user)):
        body = await request.json()
        password = (body or {}).get("password") or ""
        doc = await db.users.find_one({"_id": ObjectId(user["id"])})
        if not doc or not verify_password(password, doc["password_hash"]):
            raise HTTPException(status_code=401, detail="Password incorrect")
        await db.users.update_one({"_id": ObjectId(user["id"])},
                                  {"$set": {"mfa_enabled": False},
                                   "$unset": {"mfa_secret": "", "mfa_secret_enc": ""}})
        await db.recovery_codes.delete_many({"user_id": user["id"]})
        return {"ok": True}

    @router.post("/refresh")
    async def refresh(request: Request, response: Response):
        body = await request.json()
        raw = (body or {}).get("refresh_token", "")
        if not raw:
            raise HTTPException(status_code=401, detail="Missing refresh token")
        try:
            user_id, new_refresh = await _rotate_refresh(db, raw)
        except HTTPException:
            raise
        user = await db.users.find_one({"_id": ObjectId(user_id)})
        if not user:
            raise HTTPException(status_code=401, detail="User not found")
        access = create_access_token(str(user["_id"]), user["email"], user["role"])
        _set_cookie(response, access)
        return {"token": access, "refresh_token": new_refresh}

    @router.post("/logout")
    async def logout(request: Request, response: Response,
                     user: dict = Depends(get_current_user)):
        try:
            body = await request.json()
        except Exception:  # noqa: BLE001 — empty body is fine
            body = {}
        raw = (body or {}).get("refresh_token")
        if raw:
            await _revoke_refresh(db, hashlib.sha256(raw.encode()).hexdigest())
        # belt & braces: revoke every live session for this user on explicit logout-all flag
        if (body or {}).get("all"):
            await db.refresh_tokens.update_many(
                {"user_id": user["id"], "revoked": False},
                {"$set": {"revoked": True}})
        response.delete_cookie("access_token", path="/")
        return {"ok": True}

    @router.post("/change-password")
    async def change_password(request: Request,
                              user: dict = Depends(get_current_user)):
        body = await request.json()
        current, nxt = (body or {}).get("current"), (body or {}).get("next") or ""
        if not current or len(nxt) < 8:
            raise HTTPException(status_code=422,
                                detail="next password must be >= 8 chars")
        doc = await db.users.find_one({"_id": ObjectId(user["id"])})
        if not doc or not verify_password(current, doc["password_hash"]):
            raise HTTPException(status_code=401, detail="Current password incorrect")
        await db.users.update_one({"_id": doc["_id"]},
                                  {"$set": {"password_hash": hash_password(nxt)}})
        # kill every existing session — password change invalidates all refresh tokens
        await db.refresh_tokens.update_many(
            {"user_id": user["id"], "revoked": False},
            {"$set": {"revoked": True}})
        await _record_failure_reset(db, user["email"])
        return {"ok": True, "message": "All sessions revoked; log in again."}

    async def _record_failure_reset(db, email):
        await db.auth_failures.delete_one({"email": email})

    @router.get("/me")
    async def me(user: dict = Depends(get_current_user)):
        return user

    # ------------------------------------------------------------------ SSO
    @router.get("/sso/config")
    async def sso_config():
        import sso as sso_mod
        return {"enabled": sso_mod.sso_enabled()}

    @router.get("/sso/login")
    async def sso_login():
        import sso as sso_mod
        if not sso_mod.sso_enabled():
            raise HTTPException(status_code=501,
                                detail="SSO not configured (set OIDC_* and PUBLIC_BASE_URL)")
        state = sso_mod.make_state()
        url = sso_mod.authorize_url(sso_mod._discovery(), state)
        response = Response(status_code=302)
        response.headers["Location"] = url
        response.set_cookie("sso_state", state, httponly=True,
                            max_age=sso_mod.STATE_TTL_S, path="/",
                            secure=os.environ.get("COOKIE_SECURE", "true").lower()
                            in ("1", "true", "yes"))
        return response

    @router.get("/sso/callback")
    async def sso_callback(request: Request, code: str = None, state: str = None):
        import sso as sso_mod
        if not sso_mod.sso_enabled():
            raise HTTPException(status_code=501, detail="SSO not configured")
        cookie_state = request.cookies.get("sso_state")
        if not code or not state or not cookie_state or \
                not hmac.compare_digest(state, cookie_state) or \
                not sso_mod.verify_state(state):
            raise HTTPException(status_code=400, detail="Invalid SSO state")
        tokens = sso_mod.exchange_code(sso_mod._discovery(), code)
        email = sso_mod.fetch_email(sso_mod._discovery(), tokens["access_token"])
        if not email:
            raise HTTPException(status_code=400, detail="IdP did not return an email")
        uid, email, name, role = await sso_mod.upsert_sso_user(db, email)
        access = create_access_token(uid, email, role)
        refresh, _th = await _issue_refresh(db, uid)
        return {"id": uid, "email": email, "name": name, "role": role,
                "token": access, "refresh_token": refresh}

    router.get_current_user = get_current_user  # expose for reuse
    return router


def require_roles(get_current_user, *roles):
    async def dep(user: dict = Depends(get_current_user)):
        if user["role"] not in roles and user["role"] != "admin":
            raise HTTPException(status_code=403, detail="Insufficient permissions for this action")
        return user
    return dep


async def seed_users(db):
    if os.environ.get("DEMO_SEEDS", "true").lower() in ("0", "false", "no"):
        return
    demo = [
        ("admin@recon.io", os.environ.get("ADMIN_PASSWORD", "admin123"), "Aditi Rao", "admin"),
        ("controller@recon.io", "controller123", "Vikram Nair", "controller"),
        ("compliance@recon.io", "compliance123", "Meera Shah", "compliance"),
        ("analyst@recon.io", "analyst123", "Rohan Iyer", "analyst"),
        ("support@recon.io", "support123", "Sara Khan", "support"),
    ]
    for email, pw, name, role in demo:
        existing = await db.users.find_one({"email": email})
        if not existing:
            await db.users.insert_one({
                "email": email, "password_hash": hash_password(pw), "name": name,
                "role": role, "created_at": datetime.now(timezone.utc).isoformat(),
            })
        elif not verify_password(pw, existing["password_hash"]):
            await db.users.update_one({"email": email},
                                      {"$set": {"password_hash": hash_password(pw)}})
