"""Secret store abstraction for connector credentials.

Backends:
  - MongoVaultStore (default): AES-GCM encrypted at rest, key derived from
    JWT_SECRET via HKDF-like extraction. Secrets never appear in plaintext
    in the database or logs.
  - EnvSecretStore: reads from process env (K8s secrets / cloud AMMs inject
    these). Values are not writable — put() raises, so rotation happens
    outside the app.

Upgrade path documented in PRODUCTION.md: swap get_store() for a
HashiCorp Vault transit/KV client without touching call sites.
"""
import base64
import hashlib
import os

from cryptography.hazmat.primitives.ciphers.aead import AESGCM


class SecretStore:
    async def get(self, name): ...
    async def put(self, name, value): ...
    async def delete(self, name): ...


class MongoVaultStore(SecretStore):
    def __init__(self, db):
        self.db = db

    @staticmethod
    def _key():
        salt = b"recon-vault-v1"
        return hashlib.pbkdf2_hmac(
            "sha256", os.environ["JWT_SECRET"].encode(), salt, 200_000, dklen=32)

    def _enc(self, plain: str) -> str:
        aes = AESGCM(self._key())
        nonce = os.urandom(12)
        blob = aes.encrypt(nonce, plain.encode(), None)
        return base64.b64encode(nonce + blob).decode()

    def _dec(self, blob: str) -> str:
        raw = base64.b64decode(blob)
        aes = AESGCM(self._key())
        return aes.decrypt(raw[:12], raw[12:], None).decode()

    async def put(self, name, value):
        await self.db.vault_secrets.update_one(
            {"name": name},
            {"$set": {"blob": self._enc(value),
                      "updated_at": __import__("datetime").datetime
                      .now(__import__("datetime").timezone.utc).isoformat()}},
            upsert=True)

    async def get(self, name):
        doc = await self.db.vault_secrets.find_one({"name": name})
        if not doc:
            return None
        try:
            return self._dec(doc["blob"])
        except Exception:  # noqa: BLE001 — key rotated / corrupted
            return None

    async def delete(self, name):
        await self.db.vault_secrets.delete_one({"name": name})


class EnvSecretStore(SecretStore):
    PREFIX = "SECRET_"

    async def get(self, name):
        return os.environ.get(self.PREFIX + name.upper())

    async def put(self, name, value):
        raise RuntimeError("env-backed store is read-only; rotate via deployment")

    async def delete(self, name):
        raise RuntimeError("env-backed store is read-only")


def get_store(db=None):
    backend = os.environ.get("VAULT_BACKEND", "mongo").lower()
    if backend == "env":
        return EnvSecretStore()
    return MongoVaultStore(db)
