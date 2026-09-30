"""Client for the chat service's internal HTTP API (docs/work/README.md, Deep Dive §3).

The agent's only write path to the thread is `submit_message` (D-004, D-009), idempotent on
`client_msg_id = run_id`, so retrying a commit can never post twice.
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from typing import Any, Protocol

import httpx
from pydantic import ValidationError

from huddle_contracts.errors import ErrorCode
from huddle_contracts.internal import (
    SERVICE_TOKEN_HEADER,
    AckDeliveryRequest,
    BudgetState,
    MembershipCheck,
    RecordUsageRequest,
    Sender,
    SetAgentStatusRequest,
    SubmitMessageError,
    SubmitMessageRequest,
    SubmitMessageResponse,
)
from huddle_contracts.models import AgentState, SenderType

UNREACHABLE = "CHAT_UNREACHABLE"


class ChatError(Exception):
    def __init__(
        self,
        code: str,
        *,
        retryable: bool,
        status: int | None = None,
        retry_after_ms: int | None = None,
    ) -> None:
        super().__init__(f"{code} (http {status})")
        self.code = code
        self.retryable = retryable
        self.status = status
        self.retry_after_ms = retry_after_ms


class ChatClient(Protocol):
    async def submit_message(
        self, session_id: str, agent_id: str, client_msg_id: str, body: str, trace_id: str | None
    ) -> SubmitMessageResponse: ...

    async def ack_read(self, session_id: str, agent_id: str, up_to_seq: int) -> None: ...

    async def set_status(self, session_id: str, state: AgentState) -> None: ...

    async def record_usage(self, session_id: str, run_id: str, tokens: int) -> None: ...

    async def get_budget(self, session_id: str) -> BudgetState: ...

    async def display_name(self, session_id: str, participant_id: str) -> str | None: ...


Sleep = Callable[[float], Awaitable[None]]


class HttpChatClient:
    def __init__(
        self,
        base_url: str,
        service_token: str,
        *,
        client: httpx.AsyncClient | None = None,
        attempts: int = 3,
        backoff_s: tuple[float, ...] = (0.2, 0.5, 1.0),
        timeout_s: float = 5.0,
        sleep: Sleep = asyncio.sleep,
    ) -> None:
        # A caller-supplied client (tests: an ASGI/mock transport) must carry its own base_url.
        self._client = client or httpx.AsyncClient(base_url=base_url, timeout=timeout_s)
        self._headers = {SERVICE_TOKEN_HEADER: service_token}
        self._attempts = max(1, attempts)
        self._backoff = backoff_s
        self._sleep = sleep
        self._names: dict[tuple[str, str], str | None] = {}

    async def aclose(self) -> None:
        await self._client.aclose()

    # ---- plumbing -------------------------------------------------------------------------

    @staticmethod
    def _retry_after_header(resp: httpx.Response) -> int | None:
        raw = resp.headers.get("Retry-After")
        try:
            return int(float(raw) * 1000) if raw else None
        except ValueError:
            return None  # an HTTP-date; fall back to the body or our own backoff

    @classmethod
    def _error(cls, resp: httpx.Response) -> ChatError:
        header_ms = cls._retry_after_header(resp)
        try:
            err = SubmitMessageError.model_validate(resp.json()).error
            return ChatError(
                err.code.value,
                retryable=err.retryable,
                status=resp.status_code,
                retry_after_ms=err.retry_after_ms or header_ms,
            )
        except (ValueError, ValidationError):
            return ChatError(
                ErrorCode.INTERNAL.value,
                retryable=resp.status_code >= 500 or resp.status_code == 429,
                status=resp.status_code,
                retry_after_ms=header_ms,
            )

    async def _request(
        self,
        method: str,
        path: str,
        *,
        json: Any = None,
        params: dict[str, Any] | None = None,
        idempotent: bool = True,
    ) -> httpx.Response:
        last = ChatError(UNREACHABLE, retryable=True)
        for attempt in range(self._attempts):
            if attempt:
                delay = self._backoff[min(attempt - 1, len(self._backoff) - 1)]
                if last.retry_after_ms:
                    delay = max(delay, last.retry_after_ms / 1000)
                await self._sleep(delay)
            try:
                resp = await self._client.request(
                    method, path, json=json, params=params, headers=self._headers
                )
            except httpx.ConnectError:
                last = ChatError(UNREACHABLE, retryable=True)
                continue
            except httpx.TransportError:
                # The request may have been processed; only idempotent calls are retried.
                last = ChatError(UNREACHABLE, retryable=True)
                if not idempotent:
                    raise last from None
                continue
            if resp.is_success:
                return resp
            last = self._error(resp)
            if not last.retryable or not idempotent:
                raise last
        raise last

    # ---- API ------------------------------------------------------------------------------

    async def submit_message(
        self, session_id: str, agent_id: str, client_msg_id: str, body: str, trace_id: str | None
    ) -> SubmitMessageResponse:
        req = SubmitMessageRequest(
            session_id=session_id,
            sender=Sender(type=SenderType.AGENT, id=agent_id),
            client_msg_id=client_msg_id,
            body=body,
            trace_id=trace_id,
        )
        resp = await self._request("POST", "/internal/messages", json=req.model_dump(mode="json"))
        return SubmitMessageResponse.model_validate(resp.json())

    async def ack_read(self, session_id: str, agent_id: str, up_to_seq: int) -> None:
        req = AckDeliveryRequest(
            participant_id=agent_id, session_id=session_id, up_to_seq=up_to_seq, kind="read"
        )
        await self._request("POST", "/internal/acks", json=req.model_dump(mode="json"))

    async def set_status(self, session_id: str, state: AgentState) -> None:
        req = SetAgentStatusRequest(state=state)
        await self._request(
            "PUT", f"/internal/sessions/{session_id}/agent/status", json=req.model_dump(mode="json")
        )

    async def record_usage(self, session_id: str, run_id: str, tokens: int) -> None:
        req = RecordUsageRequest(run_id=run_id, tokens=tokens)
        await self._request(
            "POST",
            f"/internal/sessions/{session_id}/agent/usage",
            json=req.model_dump(mode="json"),
            idempotent=False,
        )

    async def get_budget(self, session_id: str) -> BudgetState:
        resp = await self._request("GET", f"/internal/sessions/{session_id}/budget")
        return BudgetState.model_validate(resp.json())

    async def display_name(self, session_id: str, participant_id: str) -> str | None:
        key = (session_id, participant_id)
        if key in self._names:
            return self._names[key]
        try:
            resp = await self._request(
                "GET", f"/internal/sessions/{session_id}/members/{participant_id}"
            )
        except ChatError:
            return None  # not cached: try again next time
        name = MembershipCheck.model_validate(resp.json()).display_name
        self._names[key] = name
        return name
