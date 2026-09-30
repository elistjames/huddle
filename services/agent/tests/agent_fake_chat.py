"""A fake chat service implementing the documented internal HTTP API (docs/work/README.md).

The real chat service is built in parallel; the agent is tested against this stand-in. It is
backed either by the in-memory world or (integration tests) by the real Postgres tables.
"""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass, field
from typing import Protocol

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from huddle_agent.chat_client import ChatError
from huddle_agent.memory import MemoryChat, MemoryWorld
from huddle_contracts.errors import ErrorBody, ErrorCode
from huddle_contracts.internal import (
    SERVICE_TOKEN_HEADER,
    AckDeliveryRequest,
    BudgetState,
    MembershipCheck,
    RecordUsageRequest,
    SetAgentStatusRequest,
    SubmitMessageError,
    SubmitMessageRequest,
    SubmitMessageResponse,
)
from huddle_contracts.models import AgentState

SERVICE_TOKEN = "svc-test-token"


class Backend(Protocol):
    async def submit(self, req: SubmitMessageRequest) -> SubmitMessageResponse: ...

    async def ack(self, req: AckDeliveryRequest) -> None: ...

    async def status(self, session_id: str, state: AgentState) -> None: ...

    async def usage(self, session_id: str, req: RecordUsageRequest) -> None: ...

    async def budget(self, session_id: str) -> BudgetState: ...

    async def member(self, session_id: str, participant_id: str) -> MembershipCheck: ...


class MemoryBackend:
    def __init__(self, world: MemoryWorld) -> None:
        self.world = world
        self.chat = MemoryChat(world)

    async def submit(self, req: SubmitMessageRequest) -> SubmitMessageResponse:
        return await self.chat.submit_message(
            req.session_id, req.sender.id, req.client_msg_id, req.body, req.trace_id
        )

    async def ack(self, req: AckDeliveryRequest) -> None:
        await self.chat.ack_read(req.session_id, req.participant_id, req.up_to_seq)

    async def status(self, session_id: str, state: AgentState) -> None:
        await self.chat.set_status(session_id, state)

    async def usage(self, session_id: str, req: RecordUsageRequest) -> None:
        await self.chat.record_usage(session_id, req.run_id, req.tokens)

    async def budget(self, session_id: str) -> BudgetState:
        return await self.chat.get_budget(session_id)

    async def member(self, session_id: str, participant_id: str) -> MembershipCheck:
        m = self.world.members.get(session_id, {}).get(participant_id)
        if m is None:
            return MembershipCheck(is_active=False)
        return MembershipCheck(
            is_active=m.status == "active",
            participant_type=m.participant_type,
            display_name=m.display_name,
            last_delivered_seq=m.last_delivered_seq,
        )


@dataclass
class Faults:
    """Scripted failures, consumed in order, keyed by route name."""

    before: dict[str, deque[int]] = field(default_factory=dict[str, deque[int]])
    after_commit: int = 0  # commit the message, then answer 503 this many times
    retry_after: str | None = None  # Retry-After header on injected failures
    calls: list[tuple[str, dict[str, object]]] = field(
        default_factory=list[tuple[str, dict[str, object]]]
    )

    def fail(self, route: str, *statuses: int) -> Faults:
        self.before.setdefault(route, deque()).extend(statuses)
        return self


_STATUS_CODE = {
    500: ErrorCode.INTERNAL,
    503: ErrorCode.PERSISTENCE_UNAVAILABLE,
    429: ErrorCode.RATE_LIMITED,
    409: ErrorCode.SESSION_CLOSED,
    403: ErrorCode.NOT_MEMBER,
    401: ErrorCode.UNAUTHENTICATED,
}


def _error(status: int, code: ErrorCode | str) -> JSONResponse:
    ec = (
        code if isinstance(code, ErrorCode) else ErrorCode.__members__.get(code, ErrorCode.INTERNAL)
    )
    body = SubmitMessageError(error=ErrorBody.of(ec))
    return JSONResponse(body.model_dump(mode="json"), status_code=status)


def create_fake_chat_app(backend: Backend, faults: Faults | None = None) -> FastAPI:
    app = FastAPI()
    f = faults or Faults()

    def guard(request: Request, route: str) -> JSONResponse | None:
        if request.headers.get(SERVICE_TOKEN_HEADER) != SERVICE_TOKEN:
            return _error(401, ErrorCode.UNAUTHENTICATED)
        queue = f.before.get(route)
        if queue:
            status = queue.popleft()
            if status == 502:
                return JSONResponse({"detail": "bad gateway"}, status_code=502)
            resp = _error(status, _STATUS_CODE.get(status, ErrorCode.INTERNAL))
            if f.retry_after is not None:
                resp.headers["Retry-After"] = f.retry_after
            return resp
        return None

    def as_http(exc: ChatError) -> JSONResponse:
        return _error(exc.status or 500, exc.code)

    @app.post("/internal/messages")
    async def submit(request: Request) -> JSONResponse:  # pyright: ignore[reportUnusedFunction]
        if (err := guard(request, "submit")) is not None:
            return err
        req = SubmitMessageRequest.model_validate(await request.json())
        f.calls.append(("submit", req.model_dump()))
        try:
            resp = await backend.submit(req)
        except ChatError as exc:
            return as_http(exc)
        if f.after_commit > 0:
            f.after_commit -= 1
            return _error(503, ErrorCode.PERSISTENCE_UNAVAILABLE)
        return JSONResponse(resp.model_dump(mode="json"))

    @app.post("/internal/acks")
    async def acks(request: Request) -> JSONResponse:  # pyright: ignore[reportUnusedFunction]
        if (err := guard(request, "ack")) is not None:
            return err
        req = AckDeliveryRequest.model_validate(await request.json())
        f.calls.append(("ack", req.model_dump()))
        await backend.ack(req)
        return JSONResponse({})

    @app.put("/internal/sessions/{sid}/agent/status")
    async def status(sid: str, request: Request) -> JSONResponse:  # pyright: ignore[reportUnusedFunction]
        if (err := guard(request, "status")) is not None:
            return err
        req = SetAgentStatusRequest.model_validate(await request.json())
        f.calls.append(("status", {"session_id": sid, **req.model_dump()}))
        await backend.status(sid, req.state)
        return JSONResponse({})

    @app.post("/internal/sessions/{sid}/agent/usage")
    async def usage(sid: str, request: Request) -> JSONResponse:  # pyright: ignore[reportUnusedFunction]
        if (err := guard(request, "usage")) is not None:
            return err
        req = RecordUsageRequest.model_validate(await request.json())
        f.calls.append(("usage", {"session_id": sid, **req.model_dump()}))
        await backend.usage(sid, req)
        return JSONResponse({})

    @app.get("/internal/sessions/{sid}/budget")
    async def budget(sid: str, request: Request) -> JSONResponse:  # pyright: ignore[reportUnusedFunction]
        if (err := guard(request, "budget")) is not None:
            return err
        return JSONResponse((await backend.budget(sid)).model_dump(mode="json"))

    @app.get("/internal/sessions/{sid}/members/{pid}")
    async def member(sid: str, pid: str, request: Request) -> JSONResponse:  # pyright: ignore[reportUnusedFunction]
        if (err := guard(request, "member")) is not None:
            return err
        f.calls.append(("member", {"session_id": sid, "participant_id": pid}))
        return JSONResponse((await backend.member(sid, pid)).model_dump(mode="json"))

    return app
