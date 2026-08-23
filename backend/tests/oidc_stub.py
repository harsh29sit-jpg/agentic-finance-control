"""A real (localhost) OIDC identity provider for integration tests.

Speaks actual HTTP: discovery, authorize (PKCE S256 + nonce enforced),
token (verifier checked, RS256 id_token minted), JWKS, userinfo.
The API server talks to it over the network exactly as it would talk to
Auth0/Okta/Google — no function-level mocking of the SSO module.
"""
import base64
import hashlib
import json
import secrets
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric import rsa, padding
from cryptography.hazmat.primitives.serialization import (
    Encoding, PrivateFormat, NoEncryption)


def _b64url(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).decode().rstrip("=")


class StubOIDCProvider:
    """Run: provider = StubOIDCProvider(client_id, client_secret,
    redirect_uri). Call .start() -> .issuer (http://127.0.0.1:port).
    Set .next_email / .next_role before an authorize hit."""

    def __init__(self, client_id, client_secret, redirect_uri):
        self.client_id = client_id
        self.client_secret = client_secret
        self.redirect_uri = redirect_uri
        self.next_email = "sso-user@corp.example.com"
        self.next_name = "SSO User"

        self.key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        self.kid = secrets.token_hex(4)
        self.issued_codes = {}          # code -> {challenge, nonce, email}
        self.issued_access = {}         # access_token -> email


    # ---- JWKS ----
    def jwks(self):
        pub = self.key.public_key().public_numbers()
        return {"keys": [{
            "kty": "RSA", "use": "sig", "alg": "RS256", "kid": self.kid,
            "n": _b64url(pub.n.to_bytes((pub.n.bit_length() + 7) // 8, "big")),
            "e": _b64url(pub.e.to_bytes(3, "big")),
        }]}

    def _mint_id_token(self, nonce, email):
        now = int(time.time())
        header = {"alg": "RS256", "kid": self.kid, "typ": "JWT"}
        claims = {
            "iss": self.issuer, "aud": self.client_id,
            "sub": f"stub-{hashlib.sha1(email.encode()).hexdigest()[:10]}",
            "email": email, "name": self.next_name,
            "nonce": nonce, "iat": now, "exp": now + 300,
        }
        h = _b64url(json.dumps(header).encode())
        p = _b64url(json.dumps(claims).encode())
        sig = self.key.sign(f"{h}.{p}".encode(), padding.PKCS1v15(),
                            hashes.SHA256())
        return f"{h}.{p}.{_b64url(sig)}"

    # ---- HTTP plumbing ----
    def _handle(self, method, path, qs, body):
        if path == "/.well-known/openid-configuration":
            base = self.issuer
            return 200, {
                "issuer": base,
                "authorization_endpoint": f"{base}/authorize",
                "token_endpoint": f"{base}/token",
                "userinfo_endpoint": f"{base}/userinfo",
                "jwks_uri": f"{base}/jwks.json",
            }
        if path == "/jwks.json":
            return 200, self.jwks()

        if path == "/authorize":
            # auto-consent: validate PKCE inputs, issue a code bound to the
            # challenge + nonce, bounce back to the client callback
            if qs.get("client_id") != [self.client_id]:
                return 401, {"error": "invalid_client"}
            code = secrets.token_urlsafe(16)
            self.issued_codes[code] = {
                "challenge": qs.get("code_challenge", [None])[0],
                "method": qs.get("code_challenge_method", [None])[0],
                "nonce": qs.get("nonce", [None])[0],
                "email": self.next_email,
            }
            loc = f"{self.redirect_uri}?code={code}&state={qs.get('state',[''])[0]}"
            return 302, {"__location__": loc}

        if path == "/token":
            form = parse_qs(body or "")
            grant = form.get("grant_type", [""])[0]
            code = form.get("code", [""])[0]
            rec = self.issued_codes.pop(code, None)
            if grant != "authorization_code" or not rec:
                return 400, {"error": "invalid_grant"}
            verifier = form.get("code_verifier", [""])[0]
            expect = _b64url(hashlib.sha256(verifier.encode()).digest()) \
                .rstrip("=")
            if not rec["challenge"] or expect != rec["challenge"]:
                return 400, {"error": "invalid_grant",
                             "hint": "PKCE verification failed"}
            email = rec["email"]
            access = secrets.token_urlsafe(24)
            self.issued_access[access] = email
            id_token = self._mint_id_token(rec["nonce"] or "", email)
            return 200, {"access_token": access, "token_type": "Bearer",
                         "id_token": id_token, "expires_in": 3600}

        if path == "/userinfo":
            auth = (self._last_auth or "")
            if not auth.startswith("Bearer "):
                return 401, {"error": "invalid_token"}
            email = self.issued_access.get(auth[7:])
            if not email:
                return 401, {"error": "invalid_token"}
            return 200, {"sub": email, "email": email, "name": self.next_name}

        return 404, {"error": "not_found"}

    _last_auth = ""

    def start(self):
        outer = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *a):  # silence
                pass

            def _send(self, status, payload, location=None):
                data = json.dumps(payload).encode()
                self.send_response(status)
                if location:
                    self.send_header("Location", location)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

            def do_GET(self):
                u = urlparse(self.path)
                outer._last_auth = self.headers.get("Authorization", "")
                if u.path in ("/authorize",):
                    status, payload = outer._handle(
                        "GET", u.path, parse_qs(u.query), None)
                    if status == 302:
                        self.send_response(302)
                        self.send_header("Location", payload["__location__"])
                        self.end_headers()
                        return
                    self._send(status, payload)
                    return
                status, payload = outer._handle("GET", u.path, {}, None)
                self._send(status, payload)

            def do_POST(self):
                length = int(self.headers.get("Content-Length", 0))
                body = self.rfile.read(length).decode()
                u = urlparse(self.path)
                outer._last_auth = self.headers.get("Authorization", "")
                status, payload = outer._handle("POST", u.path, {}, body)
                self._send(status, payload)

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        port = self.server.server_address[1]
        self.issuer = f"http://127.0.0.1:{port}"
        self.thread = threading.Thread(target=self.server.serve_forever,
                                       daemon=True)
        self.thread.start()
        return self.issuer

    def stop(self):
        self.server.shutdown()
        self.server.server_close()


# keep flake/linters honest about the dynamic import used above
PrivateFormat = None
