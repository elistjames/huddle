"""Guest tokens (Deep Dive §8, D-010): 15 min HS256 access JWT plus a rotating 30-day refresh token.

The access token carries `sub` (user id) and `exp`; the gateway verifies it with the same
HUDDLE_JWT_SECRET. Refresh tokens are opaque random strings; only their SHA-256 is stored.
"""

from __future__ import annotations

import hashlib
import secrets
from datetime import datetime, timedelta
from typing import Any

import jwt

from huddle_chat.errors import ChatError
from huddle_contracts.errors import ErrorCode

ALGORITHM = "HS256"
ACCESS_TYPE = "access"


def issue_access_token(user_id: str, secret: str, ttl_seconds: int, now: datetime) -> str:
    claims: dict[str, Any] = {
        "sub": user_id,
        "iat": int(now.timestamp()),
        "exp": int((now + timedelta(seconds=ttl_seconds)).timestamp()),
        "typ": ACCESS_TYPE,
    }
    # PyJWT's key parameter is annotated with optional `cryptography` key types; with HS256 the
    # key is always a str, so the partially-unknown member type is irrelevant here.
    return jwt.encode(claims, secret, algorithm=ALGORITHM)  # pyright: ignore[reportUnknownMemberType]


def verify_access_token(token: str, secret: str) -> str:
    """Return the user id, or raise TOKEN_EXPIRED / UNAUTHENTICATED."""
    try:
        claims: dict[str, Any] = jwt.decode(  # pyright: ignore[reportUnknownMemberType]
            token, secret, algorithms=[ALGORITHM], options={"require": ["exp", "sub"]}
        )
    except jwt.ExpiredSignatureError as exc:
        raise ChatError(ErrorCode.TOKEN_EXPIRED, "access token expired") from exc
    except jwt.InvalidTokenError as exc:
        raise ChatError(ErrorCode.UNAUTHENTICATED, "invalid access token") from exc
    sub = claims.get("sub")
    if not isinstance(sub, str) or not sub or claims.get("typ", ACCESS_TYPE) != ACCESS_TYPE:
        raise ChatError(ErrorCode.UNAUTHENTICATED, "invalid access token")
    return sub


def new_refresh_token() -> str:
    return secrets.token_urlsafe(32)


def hash_refresh_token(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def bearer(authorization: str | None) -> str | None:
    if not authorization:
        return None
    scheme, _, value = authorization.partition(" ")
    if scheme.lower() != "bearer" or not value.strip():
        return None
    return value.strip()
