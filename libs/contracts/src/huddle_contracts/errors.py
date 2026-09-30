"""Error model (Deep Dive §6)."""

from __future__ import annotations

from enum import StrEnum

from pydantic import BaseModel


class ErrorCode(StrEnum):
    UNAUTHENTICATED = "UNAUTHENTICATED"
    TOKEN_EXPIRED = "TOKEN_EXPIRED"  # noqa: S105  # nosec B105
    NOT_MEMBER = "NOT_MEMBER"
    SESSION_CLOSED = "SESSION_CLOSED"
    MESSAGE_TOO_LONG = "MESSAGE_TOO_LONG"
    RATE_LIMITED = "RATE_LIMITED"
    PERSISTENCE_UNAVAILABLE = "PERSISTENCE_UNAVAILABLE"
    PROTOCOL_UNSUPPORTED = "PROTOCOL_UNSUPPORTED"
    VALIDATION = "VALIDATION"
    FORBIDDEN = "FORBIDDEN"
    INTERNAL = "INTERNAL"


# TOKEN_EXPIRED is retryable only after refreshing the token, so it is not "retryable" here.
RETRYABLE: frozenset[ErrorCode] = frozenset(
    {ErrorCode.RATE_LIMITED, ErrorCode.PERSISTENCE_UNAVAILABLE, ErrorCode.INTERNAL}
)


class ErrorBody(BaseModel):
    code: ErrorCode
    retryable: bool
    message: str = ""
    retry_after_ms: int | None = None
    client_msg_id: str | None = None

    @classmethod
    def of(
        cls,
        code: ErrorCode,
        message: str = "",
        *,
        retry_after_ms: int | None = None,
        client_msg_id: str | None = None,
    ) -> ErrorBody:
        return cls(
            code=code,
            retryable=code in RETRYABLE,
            message=message,
            retry_after_ms=retry_after_ms,
            client_msg_id=client_msg_id,
        )
