"""HTTP routes: public REST (Deep Dive §6) and the internal API (docs/work/README.md, §3)."""

from __future__ import annotations

import hmac
from typing import Annotated

from fastapi import APIRouter, Depends, Header, Query, Request
from pydantic import BaseModel

from huddle_chat import auth
from huddle_chat.config import Settings
from huddle_chat.errors import ChatError
from huddle_chat.obs import trace
from huddle_chat.service import MAX_PAGE, AckResult, ChatService, IssuedTokens
from huddle_contracts.errors import ErrorCode
from huddle_contracts.internal import (
    SERVICE_TOKEN_HEADER,
    AckDeliveryRequest,
    BudgetState,
    MembershipCheck,
    MessagePage,
    RecordUsageRequest,
    Sender,
    SetAgentStatusRequest,
    SubmitMessageRequest,
    SubmitMessageResponse,
)
from huddle_contracts.models import AgentView, SenderType, SessionView

DEFAULT_PUBLIC_PAGE = 50


async def get_service(request: Request) -> ChatService:
    service: ChatService | None = getattr(request.app.state, "service", None)
    if service is None:
        raise ChatError(ErrorCode.INTERNAL, "service is starting")
    return service


Service = Annotated[ChatService, Depends(get_service)]


# ---- public REST models --------------------------------------------------------------------


class GuestRequest(BaseModel):
    display_name: str


class TokenResponse(BaseModel):
    user_id: str
    access_token: str
    refresh_token: str
    expires_in: int

    @classmethod
    def of(cls, tokens: IssuedTokens, expires_in: int) -> TokenResponse:
        return cls(
            user_id=tokens.user_id,
            access_token=tokens.access_token,
            refresh_token=tokens.refresh_token,
            expires_in=expires_in,
        )


class RefreshRequest(BaseModel):
    refresh_token: str


class PostMessageRequest(BaseModel):
    client_msg_id: str
    body: str


class PatchAgentRequest(BaseModel):
    status: str | None = None  # "paused" | "active" ("following" is accepted as "active")
    name: str | None = None


async def current_user(
    request: Request, authorization: Annotated[str | None, Header()] = None
) -> str:
    token = auth.bearer(authorization)
    if token is None:
        raise ChatError(ErrorCode.UNAUTHENTICATED, "missing bearer token")
    settings: Settings = request.app.state.settings
    return auth.verify_access_token(token, settings.jwt_secret)


User = Annotated[str, Depends(current_user)]

public = APIRouter()


@public.post("/auth/guest")
async def guest(req: GuestRequest, service: Service) -> TokenResponse:
    tokens = await service.guest_join(req.display_name)
    return TokenResponse.of(tokens, service.settings.access_ttl_seconds)


@public.post("/auth/refresh")
async def refresh(
    service: Service,
    body: RefreshRequest | None = None,
    authorization: Annotated[str | None, Header()] = None,
) -> TokenResponse:
    """The refresh token is the credential: `Authorization: Bearer <refresh_token>` or a JSON body
    `{refresh_token}`."""
    token = auth.bearer(authorization) or (body.refresh_token if body else None)
    if not token:
        raise ChatError(ErrorCode.UNAUTHENTICATED, "missing refresh token")
    tokens = await service.refresh(token)
    return TokenResponse.of(tokens, service.settings.access_ttl_seconds)


@public.get("/sessions/{session_id}")
async def get_session(session_id: str, user: User, service: Service) -> SessionView:
    return await service.session_view(session_id, user)


@public.get("/sessions/{session_id}/messages")
async def list_messages(
    session_id: str,
    user: User,
    service: Service,
    after_seq: int | None = None,
    before_seq: int | None = None,
    limit: Annotated[int, Query()] = DEFAULT_PUBLIC_PAGE,
) -> MessagePage:
    return await service.page(
        session_id, after_seq=after_seq, before_seq=before_seq, limit=limit, member_id=user
    )


@public.post("/sessions/{session_id}/messages")
async def post_message(
    session_id: str, req: PostMessageRequest, user: User, service: Service
) -> SubmitMessageResponse:
    """Fallback send path for when the socket is down; idempotent like the socket path."""
    return await service.submit_message(
        SubmitMessageRequest(
            session_id=session_id,
            sender=Sender(type=SenderType.USER, id=user),  # identity from the token only (§8)
            client_msg_id=req.client_msg_id,
            body=req.body,
            trace_id=trace().traceparent,
        )
    )


@public.get("/sessions/{session_id}/agent")
async def get_agent(session_id: str, user: User, service: Service) -> AgentView:
    return await service.agent_view(session_id, user)


@public.patch("/sessions/{session_id}/agent")
async def patch_agent(
    session_id: str, req: PatchAgentRequest, user: User, service: Service
) -> AgentView:
    return await service.patch_agent(session_id, user, status=req.status, name=req.name)


# ---- internal API (X-Service-Token) --------------------------------------------------------


async def require_service_token(
    request: Request, token: Annotated[str | None, Header(alias=SERVICE_TOKEN_HEADER)] = None
) -> None:
    settings: Settings = request.app.state.settings
    expected = settings.service_token.encode()
    if token is None or not hmac.compare_digest(token.encode(), expected):
        raise ChatError(ErrorCode.UNAUTHENTICATED, "invalid service token")


internal = APIRouter(prefix="/internal", dependencies=[Depends(require_service_token)])


@internal.post("/messages")
async def submit_message(req: SubmitMessageRequest, service: Service) -> SubmitMessageResponse:
    return await service.submit_message(req)


@internal.post("/acks")
async def ack_delivery(req: AckDeliveryRequest, service: Service) -> AckResult:
    return await service.ack_delivery(req)


@internal.get("/sessions/{session_id}/messages")
async def internal_messages(
    session_id: str,
    service: Service,
    after_seq: int | None = None,
    before_seq: int | None = None,
    limit: int = MAX_PAGE,
) -> MessagePage:
    return await service.page(session_id, after_seq=after_seq, before_seq=before_seq, limit=limit)


@internal.get("/sessions/{session_id}/members/{participant_id}")
async def membership(session_id: str, participant_id: str, service: Service) -> MembershipCheck:
    return await service.membership(session_id, participant_id)


@internal.put("/sessions/{session_id}/agent/status")
async def set_agent_status(
    session_id: str, req: SetAgentStatusRequest, service: Service
) -> AgentView:
    return await service.set_agent_status(session_id, req.state)


@internal.post("/sessions/{session_id}/agent/usage")
async def record_usage(session_id: str, req: RecordUsageRequest, service: Service) -> BudgetState:
    return await service.record_usage(session_id, req)


@internal.get("/sessions/{session_id}/budget")
async def budget(session_id: str, service: Service) -> BudgetState:
    return await service.budget_state(session_id)
