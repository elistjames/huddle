"""Access-token verification for the WebSocket `auth` frame (Deep Dive §6, §8).

Tokens are JWT HS256 signed with HUDDLE_JWT_SECRET, claims `sub` (user id) and `exp`.
"""

from __future__ import annotations

from dataclasses import dataclass

import jwt

from huddle_contracts.errors import ErrorCode

_ACCESS_TYPES = frozenset({None, "access"})


class AuthError(Exception):
    def __init__(self, code: ErrorCode, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


@dataclass(frozen=True, slots=True)
class Identity:
    user_id: str
    expires_at: float  # epoch seconds


def verify_token(token: str, secret: str) -> Identity:
    """Return the identity in a valid access token.

    Raises AuthError(TOKEN_EXPIRED) for a well-signed but expired token so the client can refresh
    and re-authenticate on the same socket, and AuthError(UNAUTHENTICATED) for anything else.
    """
    try:
        claims = jwt.decode(  # pyright: ignore[reportUnknownMemberType]
            token,
            secret,
            algorithms=["HS256"],
            options={"require": ["sub", "exp"]},
        )
    except jwt.ExpiredSignatureError as exc:
        raise AuthError(ErrorCode.TOKEN_EXPIRED, "token expired") from exc
    except jwt.InvalidTokenError as exc:
        raise AuthError(ErrorCode.UNAUTHENTICATED, "invalid token") from exc
    sub = claims.get("sub")
    exp = claims.get("exp")
    # A refresh token must not open a socket: accept only untyped or explicitly "access" tokens.
    if claims.get("typ") not in _ACCESS_TYPES or claims.get("type") not in _ACCESS_TYPES:
        raise AuthError(ErrorCode.UNAUTHENTICATED, "not an access token")
    if not isinstance(sub, str) or not sub or not isinstance(exp, int | float):
        raise AuthError(ErrorCode.UNAUTHENTICATED, "invalid token claims")
    return Identity(user_id=sub, expires_at=float(exp))
