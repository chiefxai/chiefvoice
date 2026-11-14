"""Local-only mock OIDC issuer for the Docker Compose development stack."""

from __future__ import annotations

import base64
import json
import os
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from time import time

import jwt
from cryptography.hazmat.primitives.asymmetric import rsa

ISSUER = "http://dev-oidc:9000"
AUDIENCE = "chiefvoice-local"
KEY_ID = "chiefvoice-local-dev"
PRIVATE_KEY = rsa.generate_private_key(public_exponent=65537, key_size=2048)
PUBLIC_NUMBERS = PRIVATE_KEY.public_key().public_numbers()


def _b64int(value: int) -> str:
    raw = value.to_bytes((value.bit_length() + 7) // 8, "big")
    return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")


JWKS = {
    "keys": [{"kty": "RSA", "use": "sig", "alg": "RS256", "kid": KEY_ID,
              "n": _b64int(PUBLIC_NUMBERS.n), "e": _b64int(PUBLIC_NUMBERS.e)}]
}
LOCAL_SCOPES = "accounts:create accounts:read agents:read agents:write api_keys:read api_keys:write billing:admin billing:read calls:read calls:reconcile calls:write integrations:read integrations:write platform:customers:read platform:customers:write webhooks:read webhooks:write"


class Handler(BaseHTTPRequestHandler):
    def do_GET(self) -> None:  # noqa: N802 - stdlib handler API
        if self.path == "/jwks":
            self._json(200, JWKS)
        elif self.path == "/token":
            now = int(time())
            token = jwt.encode(
                {"iss": ISSUER, "aud": AUDIENCE, "sub": "local-dev-user",
                 "tenant_id": "local-demo-tenant", "scope": LOCAL_SCOPES,
                 "iat": now, "exp": now + 3600},
                PRIVATE_KEY,
                algorithm="RS256",
                headers={"kid": KEY_ID},
            )
            self._json(200, {"access_token": token, "token_type": "Bearer",
                             "expires_in": 3600, "tenant_id": "local-demo-tenant",
                             "scope": LOCAL_SCOPES,
                             "warning": "Local development token only. Never use outside this Compose stack."})
        elif self.path == "/.well-known/openid-configuration":
            self._json(200, {"issuer": ISSUER, "jwks_uri": f"{ISSUER}/jwks",
                             "token_endpoint": f"{ISSUER}/token", "id_token_signing_alg_values_supported": ["RS256"]})
        else:
            self._json(404, {"detail": "Not found"})

    def _json(self, status: int, payload: dict) -> None:
        body = json.dumps(payload).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, format: str, *args: object) -> None:
        # Avoid logging the development bearer token URL response or request data.
        if self.path != "/token":
            super().log_message(format, *args)


if __name__ == "__main__":
    port = int(os.getenv("DEV_OIDC_PORT", "9000"))
    print(f"Local-only mock OIDC issuer listening on :{port}", flush=True)
    ThreadingHTTPServer(("0.0.0.0", port), Handler).serve_forever()
