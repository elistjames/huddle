"""Service errors mapped onto the shared error model (Deep Dive §6)."""

from __future__ import annotations

from huddle_contracts.errors import ErrorBody, ErrorCode

HTTP_STATUS: dict[ErrorCode, int] = {
    ErrorCode.UNAUTHENTICATED: 401,
    ErrorCode.TOKEN_EXPIRED: 401,
    ErrorCode.NOT_MEMBER: 403,
    ErrorCode.FORBIDDEN: 403,
    ErrorCode.SESSION_CLOSED: 409,
    ErrorCode.MESSAGE_TOO_LONG: 413,
    ErrorCode.VALIDATION: 400,
    ErrorCode.RATE_LIMITED: 429,
    ErrorCode.PERSISTENCE_UNAVAILABLE: 503,
    ErrorCode.PROTOCOL_UNSUPPORTED: 400,
    ErrorCode.INTERNAL: 500,
}


class ChatError(Exception):
    def __init__(
        self,
        code: ErrorCode,
        message: str = "",
        *,
        retry_after_ms: int | None = None,
        client_msg_id: str | None = None,
    ) -> None:
        super().__init__(f"{code}: {message}" if message else str(code))
        self.code = code
        self.message = message
        self.retry_after_ms = retry_after_ms
        self.client_msg_id = client_msg_id

    @property
    def status(self) -> int:
        return HTTP_STATUS.get(self.code, 500)

    def body(self) -> ErrorBody:
        return ErrorBody.of(
            self.code,
            self.message,
            retry_after_ms=self.retry_after_ms,
            client_msg_id=self.client_msg_id,
        )
