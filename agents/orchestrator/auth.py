"""JWT Bearer validation (pyjwt). Single-tenant: use DEFAULT_TENANT_ID; identity from sub."""

from __future__ import annotations

import os
from dataclasses import dataclass

import jwt
from fastapi import Header, HTTPException
from jwt import PyJWKClient

JWT_ALGORITHMS_RS256 = ["RS256"]
JWT_ALGORITHMS_HS256 = ["HS256"]


@dataclass(frozen=True)
class AuthContext:
    sub: str
    tenant_id: str


def _auth_disabled() -> bool:
    return os.environ.get("ORCHESTRATOR_AUTH_DISABLED", "").strip().lower() in (
        "1",
        "true",
        "yes",
    )


def _decode_jwt_verify(token: str) -> dict:
    algo = (os.environ.get("JWT_ALG", "RS256") or "RS256").strip().upper()
    audience = os.environ.get("JWT_AUDIENCE", "").strip() or None
    issuer = os.environ.get("JWT_ISSUER", "").strip() or None

    decode_kwargs: dict = {
        "algorithms": JWT_ALGORITHMS_HS256 if algo == "HS256" else JWT_ALGORITHMS_RS256,
        "options": {
            "verify_signature": True,
            "verify_aud": audience is not None,
            "verify_iss": issuer is not None,
        },
    }
    if audience is not None:
        decode_kwargs["audience"] = audience
    if issuer is not None:
        decode_kwargs["issuer"] = issuer

    if algo == "HS256":
        secret = os.environ.get("JWT_HS256_SECRET", "").strip()
        if not secret:
            raise HTTPException(
                status_code=500,
                detail="JWT_HS256_SECRET must be set when JWT_ALG=HS256",
            )
        return jwt.decode(token, secret, **decode_kwargs)

    jwks_url = os.environ.get("JWT_JWKS_URL", "").strip()
    if not jwks_url:
        raise HTTPException(
            status_code=500,
            detail="JWT_JWKS_URL must be set when JWT_ALG=RS256 (or unset JWT_ALG defaults to RS256)",
        )
    jwks_client = PyJWKClient(jwks_url)
    signing_key = jwks_client.get_signing_key_from_jwt(token)
    return jwt.decode(token, signing_key.key, **decode_kwargs)


async def get_auth_context(
    authorization: str | None = Header(None, alias="Authorization"),
) -> AuthContext:
    if _auth_disabled():
        return AuthContext(
            sub=os.environ.get("ORCHESTRATOR_DEV_USER_ID", "dev-user").strip()
            or "dev-user",
            tenant_id=os.environ.get("DEFAULT_TENANT_ID", "default").strip()
            or "default",
        )

    if not authorization or not authorization.startswith("Bearer "):
        raise HTTPException(
            status_code=401,
            detail="Authorization: Bearer <token> is required",
        )
    token = authorization[7:].strip()
    if not token:
        raise HTTPException(status_code=401, detail="Empty bearer token")

    try:
        payload = _decode_jwt_verify(token)
    except HTTPException:
        raise
    except jwt.PyJWTError as e:
        raise HTTPException(status_code=401, detail=f"Invalid token: {e}") from e

    sub = payload.get("sub")
    if not sub:
        raise HTTPException(status_code=401, detail="Token missing sub claim")

    return AuthContext(
        sub=str(sub),
        tenant_id=os.environ.get("DEFAULT_TENANT_ID", "default").strip() or "default",
    )
