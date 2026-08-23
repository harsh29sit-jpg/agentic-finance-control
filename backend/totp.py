"""TOTP (RFC 6238) using only the standard library.

Secrets are base32; codes are 6 digits over 30-second steps with a +/-1 step
verification window (clock-drift tolerance). Recovery-code handling lives in
auth.py.
"""
import base64
import hashlib
import hmac
import secrets
import time
from urllib.parse import quote

STEP_S = 30
DIGITS = 6
WINDOW = 1          # allow +/-1 step of clock drift
SECRET_BYTES = 20   # 160-bit


def generate_secret() -> str:
    raw = secrets.token_bytes(SECRET_BYTES)
    return base64.b32encode(raw).decode().rstrip("=")


def _code_at(secret_b32: str, counter: int) -> str:
    pad = "=" * ((8 - len(secret_b32) % 8) % 8)
    key = base64.b32decode(secret_b32.upper() + pad)
    msg = counter.to_bytes(8, "big")
    digest = hmac.new(key, msg, hashlib.sha1).digest()
    offset = digest[-1] & 0x0F
    binary = ((digest[offset] & 0x7F) << 24) | (digest[offset + 1] << 16) \
        | (digest[offset + 2] << 8) | digest[offset + 3]
    return str(binary % (10 ** DIGITS)).zfill(DIGITS)


def verify(secret_b32: str, code: str, window: int = WINDOW) -> bool:
    """Constant-time comparison across the allowed step window."""
    if not code or not code.strip().isdigit() or len(code.strip()) != DIGITS:
        return False
    code = code.strip()
    counter_now = int(time.time()) // STEP_S
    ok = False
    for delta in range(-window, window + 1):
        expected = _code_at(secret_b32, counter_now + delta)
        ok |= hmac.compare_digest(expected, code)
    return bool(ok)


def provisioning_uri(secret_b32: str, email: str, issuer: str) -> str:
    label = quote(f"{issuer}:{email}", safe="")
    params = f"secret={secret_b32}&issuer={quote(issuer)}&algorithm=SHA1&digits={DIGITS}&period={STEP_S}"
    return f"otpauth://totp/{label}?{params}"
