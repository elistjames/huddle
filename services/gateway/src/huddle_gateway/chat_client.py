"""Client for the chat service's internal HTTP API (docs/work/README.md, Deep Dive §3).

Every call carries `X-Service-Token`. Failures surface as ChatError holding the stable error model
(Deep Dive §6), so the gateway can forward it to the client unchanged.
"""

from __future__ import annotations

from typing import TypeVar
from urllib.parse import quote

import httpx
from pydantic import BaseModel, ValidationError

from huddle_contracts.errors import ErrorBody, ErrorCode
from huddle_contracts.internal import (
    SERVICE_TOKEN_HEADER,
    AckDeliveryRequest,
    MembershipCheck,
    MessagePage,
    SubmitMessageError,
    SubmitMessageRequest,
    SubmitMessageResponse,
)

_M = TypeVar("_M", bound=BaseModel)


class ChatError(Exception):
    def __init__(self, body: ErrorBody, status: int | None = None) -> None:
        super().__init__(f"{body.code}: {body.message}")
        self.body = body
        self.status = status


def _seg(value: str) -> str:
    """Quote an untrusted path segment so it cannot escape its position in the URL."""
    return quote(value, safe="")


def _error_from(resp: httpx.Response) -> ChatError:
    try:
        body = SubmitMessageError.model_validate_json(resp.content).error
    except ValidationError:
        if resp.status_code == 429:
            body = ErrorBody.of(ErrorCode.RATE_LIMITED, "rate limited")
        elif resp.status_code == 404:
            body = ErrorBody.of(ErrorCode.NOT_MEMBER, "not found")
        else:
            # 401/403 here means the gateway's service token is wrong: an internal fault.
            body = ErrorBody.of(ErrorCode.INTERNAL, f"chat service returned {resp.status_code}")
    return ChatError(body, resp.status_code)


class ChatClient:
    def __init__(
        self,
        base_url: str,
        service_token: str,
        *,
        timeout_s: float = 5.0,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self._http = httpx.AsyncClient(
            base_url=base_url,
            headers={SERVICE_TOKEN_HEADER: service_token},
            timeout=timeout_s,
            transport=transport,
        )

    async def aclose(self) -> None:
        await self._http.aclose()

    async def _request(
        self,
        method: str,
        url: str,
        *,
        json: str | None = None,
        params: dict[str, int] | None = None,
        traceparent: str | None = None,
    ) -> httpx.Response:
        headers: dict[str, str] = {}
        if traceparent:
            headers["traceparent"] = traceparent
        if json is not None:
            headers["content-type"] = "application/json"
        try:
            resp = await self._http.request(
                method, url, content=json, params=params, headers=headers
            )
        except httpx.TimeoutException as exc:
            raise ChatError(ErrorBody.of(ErrorCode.INTERNAL, "chat service timed out")) from exc
        except httpx.HTTPError as exc:
            raise ChatError(ErrorBody.of(ErrorCode.INTERNAL, "chat service unreachable")) from exc
        if resp.status_code >= 400:
            raise _error_from(resp)
        return resp

    async def submit_message(
        self, req: SubmitMessageRequest, *, traceparent: str | None = None
    ) -> SubmitMessageResponse:
        resp = await self._request(
            "POST", "/internal/messages", json=req.model_dump_json(), traceparent=traceparent
        )
        return self._parse(SubmitMessageResponse, resp)

    async def ack(self, req: AckDeliveryRequest) -> None:
        await self._request("POST", "/internal/acks", json=req.model_dump_json())

    async def get_messages(self, session_id: str, *, after_seq: int, limit: int) -> MessagePage:
        resp = await self._request(
            "GET",
            f"/internal/sessions/{_seg(session_id)}/messages",
            params={"after_seq": after_seq, "limit": limit},
        )
        return self._parse(MessagePage, resp)

    async def check_member(self, session_id: str, participant_id: str) -> MembershipCheck:
        try:
            resp = await self._request(
                "GET", f"/internal/sessions/{_seg(session_id)}/members/{_seg(participant_id)}"
            )
        except ChatError as err:
            if err.body.code is ErrorCode.NOT_MEMBER or err.status == 404:
                return MembershipCheck(is_active=False)
            raise
        return self._parse(MembershipCheck, resp)

    @staticmethod
    def _parse(model: type[_M], resp: httpx.Response) -> _M:
        try:
            return model.model_validate_json(resp.content)
        except ValidationError as exc:
            raise ChatError(
                ErrorBody.of(ErrorCode.INTERNAL, "malformed chat service response"),
                resp.status_code,
            ) from exc
