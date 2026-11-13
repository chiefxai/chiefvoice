import asyncio
from dataclasses import dataclass

import jwt
from fastapi import HTTPException, Request, Security, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from jwt import InvalidTokenError, PyJWKClient, PyJWKClientConnectionError, PyJWKClientError

from chiefvoice.core.config import settings

bearer_scheme = HTTPBearer(auto_error=False)


@dataclass(frozen=True, slots=True)
class Principal:
    subject: str
    tenant_id: str
    scopes: frozenset[str]
    auth_method: str = "oidc"
    api_key_id: str | None = None


class OidcTokenVerifier:
    """Verify bearer tokens using an OIDC issuer's rotating JWKS keys."""

    def __init__(
        self,
        issuer: str,
        audience: str,
        jwks_url: str,
        tenant_claim: str,
        algorithms: tuple[str, ...] = ("RS256",),
    ):
        self.issuer = issuer
        self.audience = audience
        self.tenant_claim = tenant_claim
        allowed_algorithms = {"RS256", "PS256", "ES256", "ES384", "EdDSA"}
        if not algorithms or set(algorithms) - allowed_algorithms:
            raise ValueError("OIDC algorithms must be an explicit asymmetric allowlist")
        self.algorithms = algorithms
        self.jwks = PyJWKClient(jwks_url, cache_jwk_set=True, lifespan=300)

    async def verify(self, token: str) -> Principal:
        try:
            payload = await asyncio.to_thread(self._decode, token)
        except PyJWKClientConnectionError as exc:
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail="Identity provider keys are temporarily unavailable",
            ) from exc
        except (InvalidTokenError, PyJWKClientError) as exc:
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="Invalid or expired access token",
                headers={"WWW-Authenticate": "Bearer"},
            ) from exc

        subject = payload.get("sub")
        tenant_id = payload.get(self.tenant_claim)
        raw_scopes = payload.get("scope", "")
        scopes = raw_scopes.split() if isinstance(raw_scopes, str) else []
        if not isinstance(subject, str) or not subject or not isinstance(tenant_id, str) or not tenant_id:
            raise HTTPException(status_code=401, detail="Token is missing subject or tenant claim")
        return Principal(subject=subject, tenant_id=tenant_id, scopes=frozenset(scopes))

    def _decode(self, token: str) -> dict:
        signing_key = self.jwks.get_signing_key_from_jwt(token)
        return jwt.decode(
            token,
            signing_key.key,
            algorithms=list(self.algorithms),
            audience=self.audience,
            issuer=self.issuer,
            options={"require": ["exp", "iat", "sub"]},
        )


async def current_principal(
    request: Request,
    credentials: HTTPAuthorizationCredentials | None = Security(bearer_scheme),
) -> Principal:
    if credentials is None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Bearer access token required",
            headers={"WWW-Authenticate": "Bearer"},
        )
    if credentials.credentials.startswith("cv_live_"):
        api_key_directory = getattr(request.app.state, "api_key_directory", None)
        if api_key_directory is None:
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail="API key authentication is not configured",
            )
        identity = await api_key_directory.authenticate(credentials.credentials)
        if identity is None:
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="Invalid or expired API key",
                headers={"WWW-Authenticate": "Bearer"},
            )
        return Principal(
            subject=identity.subject,
            tenant_id=identity.tenant_id,
            scopes=identity.scopes,
            auth_method="api_key",
            api_key_id=identity.key_id,
        )
    verifier: OidcTokenVerifier | None = getattr(request.app.state, "token_verifier", None)
    if verifier is None:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="OIDC authentication is not configured",
        )
    return await verifier.verify(credentials.credentials)


def require_scope(scope: str):
    async def dependency(principal: Principal = Security(current_principal)) -> Principal:
        if scope not in principal.scopes:
            raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Insufficient scope")
        return principal

    return dependency
