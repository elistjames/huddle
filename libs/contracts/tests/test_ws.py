from pydantic import TypeAdapter

from huddle_contracts.errors import ErrorBody, ErrorCode
from huddle_contracts.ws import ClientFrame, MessageSend


def test_client_frame_discriminates() -> None:
    f = TypeAdapter[MessageSend](ClientFrame).validate_python(
        {"type": "message.send", "client_msg_id": "x", "body": "hi"}
    )
    assert isinstance(f, MessageSend)
    assert f.v == 1


def test_error_retryable_flags() -> None:
    assert ErrorBody.of(ErrorCode.RATE_LIMITED).retryable
    assert not ErrorBody.of(ErrorCode.NOT_MEMBER).retryable
    assert not ErrorBody.of(ErrorCode.TOKEN_EXPIRED).retryable
