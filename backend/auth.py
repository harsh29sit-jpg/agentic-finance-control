"""JWT email/password auth + RBAC for the reconciliation platform."""
import os
import asyncio
import jwt
import bcrypt
from datetime import datetime, timezone, timedelta
from fastapi import APIRouter, HTTPException, Request, Response, Depends
from bson import ObjectId

from models import RegisterRequest, LoginRequest

JWT_ALGORITHM = "HS256"

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


def create_access_token(user_id: str, email: str, role: str) -> str:
    payload = {"sub": user_id, "email": email, "role": role,
               "exp": datetime.now(timezone.utc) + timedelta(hours=12), "type": "access"}
    return jwt.encode(payload, _secret(), algorithm=JWT_ALGORITHM)


def _cookie_secure():
    return os.environ.get("COOKIE_SECURE", "true").lower() in ("1", "true", "yes")


def _set_cookie(response: Response, token: str):
    response.set_cookie("access_token", token, httponly=True, secure=_cookie_secure(),
                        samesite="none" if _cookie_secure() else "lax",
                        max_age=43200, path="/")


def build_auth_router(db, limiter=None):
    router = APIRouter(prefix="/api/auth", tags=["auth"])

    async def _rate_ok(request: Request, key_extra: str) -> bool:
        """Shared, cross-instance rate limit (limiter may be sync or async)."""
        if limiter is None:
            return True
        ip = request.client.host if request.client else "unknown"
        result = limiter.allow(f"{ip}:{key_extra}", max_events=10, window_seconds=60)
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
               "created_at": datetime.now(timezone.utc).isoformat()}
        res = await db.users.insert_one(doc)
        uid = str(res.inserted_id)
        token = create_access_token(uid, email, role)
        _set_cookie(response, token)
        return {"id": uid, "email": email, "name": body.name, "role": role, "token": token}

    @router.post("/login")
    async def login(body: LoginRequest, request: Request, response: Response):
        if not await _rate_ok(request, body.email.lower()):
            raise HTTPException(status_code=429, detail="Too many login attempts; retry shortly")
        email = body.email.lower()
        user = await db.users.find_one({"email": email})
        if not user or not verify_password(body.password, user["password_hash"]):
            raise HTTPException(status_code=401, detail="Invalid email or password")
        uid = str(user["_id"])
        token = create_access_token(uid, email, user["role"])
        _set_cookie(response, token)
        return {"id": uid, "email": email, "name": user["name"], "role": user["role"], "token": token}

    @router.post("/logout")
    async def logout(response: Response, user: dict = Depends(get_current_user)):
        response.delete_cookie("access_token", path="/")
        return {"ok": True}

    @router.get("/me")
    async def me(user: dict = Depends(get_current_user)):
        return user

    router.get_current_user = get_current_user  # expose for reuse
    return router


def require_roles(get_current_user, *roles):
    async def dep(user: dict = Depends(get_current_user)):
        if user["role"] not in roles and user["role"] != "admin":
            raise HTTPException(status_code=403, detail="Insufficient permissions for this action")
        return user
    return dep


async def seed_users(db):
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
            await db.users.update_one({"email": email}, {"$set": {"password_hash": hash_password(pw)}})
