"""Local-only mock OIDC provider for the Docker Compose development stack."""

from __future__ import annotations

import base64
import hashlib
import html
import json
import os
import secrets
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from time import time
from urllib.parse import parse_qs, urlencode, urlparse

import jwt
from cryptography.hazmat.primitives.asymmetric import rsa

ISSUER = os.getenv("DEV_OIDC_ISSUER", "http://localhost:9000").rstrip("/")
AUDIENCE = "chiefvoice-local"
CLIENT_ID = "chiefvoice-console"
REDIRECT_URI = os.getenv("DEV_OIDC_REDIRECT_URI", "http://localhost:5173/")
KEY_ID = "chiefvoice-local-dev"
PRIVATE_KEY = rsa.generate_private_key(public_exponent=65537, key_size=2048)
LOCAL_SCOPES = (
    "accounts:create accounts:read agents:read agents:write api_keys:read api_keys:write "
    "billing:admin billing:read calls:read calls:reconcile calls:write integrations:read "
    "integrations:write platform:customers:read platform:customers:write "
    "webhooks:read webhooks:write"
)
_AUTHORIZATION_CODES: dict[str, dict[str, object]] = {}


def _b64int(value: int) -> str:
    raw = value.to_bytes((value.bit_length() + 7) // 8, "big")
    return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")


PUBLIC_NUMBERS = PRIVATE_KEY.public_key().public_numbers()
JWKS = {
    "keys": [
        {
            "kty": "RSA",
            "use": "sig",
            "alg": "RS256",
            "kid": KEY_ID,
            "n": _b64int(PUBLIC_NUMBERS.n),
            "e": _b64int(PUBLIC_NUMBERS.e),
        }
    ]
}


class Handler(BaseHTTPRequestHandler):
    def do_OPTIONS(self) -> None:  # noqa: N802 - stdlib handler API
        self.send_response(204)
        self._cors_headers()
        self.end_headers()

    def do_GET(self) -> None:  # noqa: N802 - stdlib handler API
        path = urlparse(self.path).path
        if path == "/jwks":
            self._json(200, JWKS)
        elif path == "/.well-known/openid-configuration":
            self._json(
                200,
                {
                    "issuer": ISSUER,
                    "authorization_endpoint": f"{ISSUER}/authorize",
                    "token_endpoint": f"{ISSUER}/token",
                    "jwks_uri": f"{ISSUER}/jwks",
                    "response_types_supported": ["code"],
                    "subject_types_supported": ["public"],
                    "id_token_signing_alg_values_supported": ["RS256"],
                    "grant_types_supported": ["authorization_code"],
                    "token_endpoint_auth_methods_supported": ["none"],
                    "code_challenge_methods_supported": ["S256"],
                    "scopes_supported": [
                        "openid",
                        "profile",
                        "email",
                        "platform:customers:read",
                        "platform:customers:write",
                    ],
                },
            )
        elif path == "/authorize":
            self._authorize_page()
        else:
            self._json(404, {"detail": "Not found"})

    def do_POST(self) -> None:  # noqa: N802 - stdlib handler API
        path = urlparse(self.path).path
        fields = parse_qs(self.rfile.read(int(self.headers.get("Content-Length", "0"))).decode())
        if path == "/authorize":
            self._authorize(fields)
        elif path == "/token":
            self._token(fields)
        else:
            self._json(404, {"detail": "Not found"})

    def _authorize_page(self) -> None:
        query = parse_qs(urlparse(self.path).query)
        redirect_uri = self._field(query, "redirect_uri")
        challenge = self._field(query, "code_challenge")
        state = self._field(query, "state")
        nonce = self._field(query, "nonce")
        if (
            self._field(query, "client_id") != CLIENT_ID
            or redirect_uri != REDIRECT_URI
            or self._field(query, "response_type") != "code"
            or self._field(query, "code_challenge_method") != "S256"
            or not challenge
            or not state
            or not nonce
        ):
            self._json(400, {"detail": "Invalid local OIDC authorization request"})
            return

        hidden = {
            "client_id": CLIENT_ID,
            "redirect_uri": redirect_uri,
            "response_type": "code",
            "scope": self._field(query, "scope"),
            "state": state,
            "nonce": nonce,
            "code_challenge": challenge,
            "code_challenge_method": "S256",
        }
        inputs = "".join(
            f'<input type="hidden" name="{html.escape(name)}" value="{html.escape(value)}">'
            for name, value in hidden.items()
        )
        body = f"""<!doctype html><html lang="en"><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Local sign in</title>
<style>
body {{ margin:0; min-height:100vh; display:grid; place-items:center;
  background:#101b2d; font:15px system-ui; color:#18243a }}
main {{ box-sizing:border-box; width:min(420px,calc(100% - 32px)); padding:36px;
  border-radius:20px; background:white; text-align:center; box-shadow:0 24px 80px #0004 }}
.mark {{ margin:auto auto 20px; width:48px; height:48px; border-radius:15px;
  display:grid; place-items:center; background:#72dfbd; font-size:22px }}
p {{ color:#738097; line-height:1.6 }}
button {{ width:100%; padding:13px; border:0; border-radius:10px; background:#4159d8;
  color:white; font-weight:700; font-size:14px; cursor:pointer }}
small {{ display:block; margin-top:22px; color:#9099aa }}
</style>
<main>
<div class="mark">C</div><small>LOCAL DEVELOPMENT IDENTITY</small>
<h1>Sign in to Chiefvoice</h1>
<p>This local OIDC provider signs you in as the development platform administrator.</p>
<form method="post" action="/authorize">{inputs}
<button type="submit">Continue as local admin</button></form>
<small>Development only. Do not use this identity outside your local stack.</small>
</main></html>"""
        encoded = body.encode()
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(encoded)))
        self.end_headers()
        self.wfile.write(encoded)

    def _authorize(self, fields: dict[str, list[str]]) -> None:
        redirect_uri = self._field(fields, "redirect_uri")
        if (
            self._field(fields, "client_id") != CLIENT_ID
            or redirect_uri != REDIRECT_URI
            or self._field(fields, "response_type") != "code"
            or self._field(fields, "code_challenge_method") != "S256"
        ):
            self._json(400, {"detail": "Invalid local OIDC authorization request"})
            return
        code = secrets.token_urlsafe(32)
        _AUTHORIZATION_CODES[code] = {
            "client_id": CLIENT_ID,
            "redirect_uri": redirect_uri,
            "code_challenge": self._field(fields, "code_challenge"),
            "state": self._field(fields, "state"),
            "nonce": self._field(fields, "nonce"),
            "scope": self._field(fields, "scope"),
            "expires_at": int(time()) + 120,
        }
        destination = (
            f"{redirect_uri}?{urlencode({'code': code, 'state': self._field(fields, 'state')})}"
        )
        self.send_response(302)
        self.send_header("Location", destination)
        self.send_header("Cache-Control", "no-store")
        self.end_headers()

    def _token(self, fields: dict[str, list[str]]) -> None:
        code = self._field(fields, "code")
        authorization = _AUTHORIZATION_CODES.pop(code, None)
        verifier = self._field(fields, "code_verifier")
        challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest())
        expected_challenge = challenge.decode("ascii").rstrip("=")
        if (
            self._field(fields, "grant_type") != "authorization_code"
            or self._field(fields, "client_id") != CLIENT_ID
            or authorization is None
            or authorization["client_id"] != CLIENT_ID
            or authorization["redirect_uri"] != self._field(fields, "redirect_uri")
            or authorization["code_challenge"] != expected_challenge
            or int(authorization["expires_at"]) < int(time())
        ):
            self._json(400, {"error": "invalid_grant"})
            return

        now = int(time())
        access_token = jwt.encode(
            {
                "iss": ISSUER,
                "aud": AUDIENCE,
                "sub": "local-dev-user",
                "tenant_id": "local-demo-tenant",
                "scope": LOCAL_SCOPES,
                "iat": now,
                "exp": now + 3600,
            },
            PRIVATE_KEY,
            algorithm="RS256",
            headers={"kid": KEY_ID},
        )
        id_token = jwt.encode(
            {
                "iss": ISSUER,
                "aud": CLIENT_ID,
                "sub": "local-dev-user",
                "name": "Local Platform Admin",
                "email": "admin@localhost",
                "email_verified": True,
                "nonce": authorization["nonce"],
                "iat": now,
                "exp": now + 3600,
            },
            PRIVATE_KEY,
            algorithm="RS256",
            headers={"kid": KEY_ID},
        )
        granted_scopes = "openid profile email " + LOCAL_SCOPES
        self._json(
            200,
            {
                "access_token": access_token,
                "id_token": id_token,
                "token_type": "Bearer",
                "expires_in": 3600,
                "scope": granted_scopes,
            },
        )

    @staticmethod
    def _field(fields: dict[str, list[str]], key: str) -> str:
        values = fields.get(key, [])
        return values[0] if values else ""

    def _cors_headers(self) -> None:
        origin = self.headers.get("Origin", "")
        if origin in {"http://localhost:5173", "http://127.0.0.1:5173"}:
            self.send_header("Access-Control-Allow-Origin", origin)
            self.send_header("Vary", "Origin")
            self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
            self.send_header("Access-Control-Allow-Headers", "Content-Type")
            self.send_header("Access-Control-Max-Age", "600")

    def _json(self, status: int, payload: dict) -> None:
        body = json.dumps(payload).encode()
        self.send_response(status)
        self._cors_headers()
        self.send_header("Content-Type", "application/json")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, format: str, *args: object) -> None:
        # Authorization codes and token responses are credentials; don't log request paths.
        if urlparse(self.path).path not in {"/authorize", "/token"}:
            super().log_message(format, *args)


if __name__ == "__main__":
    port = int(os.getenv("DEV_OIDC_PORT", "9000"))
    print(f"Local-only mock OIDC issuer listening on :{port}", flush=True)
    ThreadingHTTPServer(("0.0.0.0", port), Handler).serve_forever()
