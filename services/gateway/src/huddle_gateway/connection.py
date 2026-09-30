"""One client socket: auth handshake, frame dispatch, replay, heartbeats, send queue.

Deep Dive §6 (connection protocol), §2 (reconnect and catch-up), §4 (registry), §8 (trust
boundaries: the gateway stamps the authenticated identity and ignores client sender/seq/time).
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import secrets
import time
from enum import Enum
from typing import TYPE_CHECKING, Any

from pydantic import BaseModel, TypeAdapter, ValidationError
from starlette.websockets import WebSocket, WebSocketDisconnect

from huddle_contracts import PROTOCOL_VERSION
from huddle_contracts.errors import ErrorBody, ErrorCode
from huddle_contracts.internal import AckDeliveryRequest, Sender, SubmitMessageRequest
from huddle_contracts.models import MAX_BODY_CHARS, SenderType
from huddle_contracts.ws import (
    CLOSE_AUTH_TIMEOUT,
    CLOSE_SLOW_CONSUMER,
    Ack,
    Auth,
    AuthOk,
    ClientFrame,
    ErrorFrame,
    MessageAck,
    MessageSend,
    Ping,
    Pong,
    Read,
    Subscribe,
    SyncBatch,
)
from huddle_gateway import tracing
from huddle_gateway.auth import AuthError, Identity, verify_token
from huddle_gateway.chat_client import ChatError
from huddle_gateway.logs import log_event
from huddle_gateway.outbound import OutboundQueue, Outgoing, SeqWindow, merge_buffered
from huddle_gateway.registry import ConnInfo

if TYPE_CHECKING:
    from huddle_gateway.gateway import Gateway

# Deep Dive §12: the server supports protocol versions N and N-1 (there is no v0 yet).
SUPPORTED_VERSIONS = frozenset(v for v in (PROTOCOL_VERSION, PROTOCOL_VERSION - 1) if v >= 1)

# Standard close codes for cases the spec does not give a custom code for.
CLOSE_GOING_AWAY = 1001  # draining, missed heartbeats
CLOSE_PROTOCOL_ERROR = 1002  # PROTOCOL_UNSUPPORTED
CLOSE_POLICY = 1008  # UNAUTHENTICATED, too many connections for this user
CLOSE_INTERNAL = 1011  # the registry could not be written

_client_frame: TypeAdapter[Any] = TypeAdapter(ClientFrame)


class Phase(Enum):
    IDLE = "idle"  # authenticated, not subscribed: session frames are dropped
    REPLAYING = "replaying"  # catching up from the store: live session frames are buffered
    LIVE = "live"


class _Closed(Exception):
    """The peer went away."""


def new_connection_id() -> str:
    return f"conn_{secrets.token_hex(12)}"


class Connection:
    def __init__(self, gateway: Gateway, ws: WebSocket) -> None:
        s = gateway.settings
        self.gw = gateway
        self.settings = s
        self.ws = ws
        self.id = new_connection_id()
        self.queue = OutboundQueue(max_events=s.queue_max_events, max_bytes=s.queue_max_bytes)
        self.info: ConnInfo | None = None
        self.token_expires_at = 0.0
        self.token_expired_at: float | None = None  # set once TOKEN_EXPIRED has been sent
        self.last_seen = time.monotonic()
        self.session_id: str | None = None
        self.phase = Phase.IDLE
        self.buffer: list[Outgoing] = []
        self.buffer_bytes = 0
        self.replayed_through = 0
        self.recent = SeqWindow()
        self._replay_task: asyncio.Task[None] | None = None
        self._close_requested = asyncio.Event()
        self.close_code = 1000
        self.close_reason = ""
        self._flush_on_close = True
        self._peer_gone = False
        self._last_refresh = time.monotonic()
        self.closed = False

    # ---- identity ------------------------------------------------------------------------

    @property
    def user_id(self) -> str | None:
        return self.info.user_id if self.info else None

    def _authed(self) -> ConnInfo:
        # Frames other than auth are only dispatched after the handshake registered us.
        if self.info is None:
            raise RuntimeError("connection is not authenticated")
        return self.info

    def _log(self, event: str, *, level: int = logging.INFO, **fields: Any) -> None:
        log_event(
            event,
            level=level,
            gateway_id=self.settings.gateway_id,
            connection_id=self.id,
            actor_id=self.user_id,
            session_id=self.session_id,
            **fields,
        )

    # ---- outbound ------------------------------------------------------------------------

    def send(self, item: Outgoing | BaseModel) -> None:
        """Queue a frame for this socket; overflowing the queue closes it with 4008 (§6)."""
        if self._close_requested.is_set():
            return
        out = item if isinstance(item, Outgoing) else Outgoing.from_model(item)
        if not self.queue.put(out):
            self._log(
                "conn.slow_consumer",
                level=logging.WARNING,
                queued_events=len(self.queue),
                queued_bytes=self.queue.nbytes,
            )
            self.request_close(CLOSE_SLOW_CONSUMER, "slow consumer", flush=False)

    def send_error(
        self,
        code: ErrorCode,
        message: str = "",
        *,
        client_msg_id: str | None = None,
        body: ErrorBody | None = None,
    ) -> None:
        err = body or ErrorBody.of(code, message)
        if client_msg_id is not None:
            err = err.model_copy(update={"client_msg_id": client_msg_id})
        self.send(ErrorFrame(error=err))

    def deliver(self, item: Outgoing) -> None:
        """Route a fan-out frame from `gw:{gateway_id}` (Deep Dive §3, §4)."""
        if item.session_id is not None and item.session_id != self.session_id:
            return  # not subscribed to that session: the subscribe replay will cover it
        if self.phase is Phase.REPLAYING:
            self.buffer.append(item)
            self.buffer_bytes += item.nbytes
            if len(self.buffer) > self.queue.max_events or self.buffer_bytes > self.queue.max_bytes:
                self._log("conn.slow_consumer", level=logging.WARNING, buffered=len(self.buffer))
                self.request_close(CLOSE_SLOW_CONSUMER, "slow consumer", flush=False)
            return
        if item.seq is not None:
            if item.seq <= self.replayed_through or item.seq in self.recent:
                return  # duplicate of something already sent
            self.recent.add(item.seq)
        self.send(item)

    def request_close(self, code: int, reason: str, *, flush: bool = True) -> None:
        if self._close_requested.is_set():
            return
        self.close_code = code
        self.close_reason = reason
        self._flush_on_close = flush
        self._close_requested.set()

    # ---- lifecycle -----------------------------------------------------------------------

    async def serve(self) -> None:
        """Run until the socket closes. The caller has already accepted it."""
        tasks: list[asyncio.Task[Any]] = [
            asyncio.create_task(self._reader(), name=f"{self.id}-reader"),
            asyncio.create_task(self._writer(), name=f"{self.id}-writer"),
            asyncio.create_task(self._watchdog(), name=f"{self.id}-watchdog"),
        ]
        closer = asyncio.create_task(self._close_requested.wait())
        try:
            done, _ = await asyncio.wait([*tasks, closer], return_when=asyncio.FIRST_COMPLETED)
            for t in done:
                exc = None if t is closer or t.cancelled() else t.exception()
                if exc is not None:
                    self._log("conn.error", level=logging.ERROR, task=t.get_name(), exc_info=exc)
                    self.request_close(CLOSE_INTERNAL, "internal error")
            if not self._peer_gone and self._flush_on_close:
                with contextlib.suppress(TimeoutError):
                    await asyncio.wait_for(self.queue.wait_empty(), self.settings.close_grace_s)
        finally:
            closer.cancel()
            for t in tasks:
                t.cancel()
            if self._replay_task is not None:
                self._replay_task.cancel()
            await asyncio.gather(*tasks, closer, return_exceptions=True)
            await self._close_socket()
            await self._cleanup()

    async def _close_socket(self) -> None:
        if self._peer_gone and not self._close_requested.is_set():
            return  # the peer closed first
        try:
            await asyncio.wait_for(
                self.ws.close(code=self.close_code, reason=self.close_reason),
                self.settings.close_grace_s,
            )
        except (TimeoutError, RuntimeError, OSError, WebSocketDisconnect):
            # A peer that stopped reading cannot take the close frame; returning lets the
            # server drop the transport.
            pass

    async def _cleanup(self) -> None:
        self.closed = True
        if self.info is not None:
            await self.gw.unregister(self)
        self._log("conn.closed", code=self.close_code, reason=self.close_reason or None)

    async def _writer(self) -> None:
        while True:
            item = await self.queue.get()
            try:
                await self.ws.send_text(item.text)
            except (WebSocketDisconnect, RuntimeError, OSError):
                self._peer_gone = True
                return
            finally:
                self.queue.task_done()

    async def _watchdog(self) -> None:
        """Close after three missed heartbeats (§4) and police token expiry (§6)."""
        s = self.settings
        while True:
            await asyncio.sleep(s.watchdog_tick_s)
            now_mono = time.monotonic()
            if now_mono - self.last_seen > s.heartbeat_timeout_s:
                self._log("conn.heartbeat_timeout", level=logging.WARNING)
                self.request_close(CLOSE_GOING_AWAY, "missed heartbeats", flush=False)
                return
            if self.info is None:
                continue
            now = time.time()
            if self.token_expired_at is None and now >= self.token_expires_at:
                self.token_expired_at = now_mono
                self._log("auth.token_expired")
                self.send_error(ErrorCode.TOKEN_EXPIRED, "token expired; re-authenticate")
            elif (
                self.token_expired_at is not None
                and now_mono - self.token_expired_at > s.auth_timeout_s
            ):
                self._log("auth.reauth_timeout", level=logging.WARNING)
                self.request_close(CLOSE_AUTH_TIMEOUT, "re-authentication timeout")
                return

    # ---- inbound -------------------------------------------------------------------------

    async def _receive(self, limit_s: float | None = None) -> str:
        coro = self.ws.receive()
        msg = await (asyncio.wait_for(coro, limit_s) if limit_s is not None else coro)
        if msg["type"] == "websocket.disconnect":
            self._peer_gone = True
            raise _Closed
        text = msg.get("text")
        if text is None:
            data = msg.get("bytes") or b""
            text = data.decode("utf-8", errors="replace")
        self.last_seen = time.monotonic()
        return text

    def _parse(self, raw: str) -> Any | None:
        """Decode one client frame, answering protocol errors itself. None means 'skip'."""
        if len(raw.encode()) > self.settings.max_frame_bytes:
            self.send_error(ErrorCode.VALIDATION, "frame too large")
            return None
        try:
            obj: Any = json.loads(raw)
        except ValueError:
            self.send_error(ErrorCode.VALIDATION, "frame is not JSON")
            return None
        if not isinstance(obj, dict):
            self.send_error(ErrorCode.VALIDATION, "frame must be a JSON object")
            return None
        frame: dict[str, Any] = obj  # pyright: ignore[reportUnknownVariableType]
        version = frame.get("v", PROTOCOL_VERSION)
        if version not in SUPPORTED_VERSIONS or isinstance(version, bool):
            self._log("conn.protocol_unsupported", level=logging.WARNING, version=str(version))
            self.send_error(
                ErrorCode.PROTOCOL_UNSUPPORTED, f"supported: {sorted(SUPPORTED_VERSIONS)}"
            )
            self.request_close(CLOSE_PROTOCOL_ERROR, "protocol unsupported")
            return None
        try:
            return _client_frame.validate_python(frame)
        except ValidationError:
            cmid = frame.get("client_msg_id")
            self.send_error(
                ErrorCode.VALIDATION,
                "invalid frame",
                client_msg_id=cmid if isinstance(cmid, str) else None,
            )
            return None

    async def _reader(self) -> None:
        try:
            if not await self._handshake():
                return
            while True:
                frame = self._parse(await self._receive())
                if frame is not None:
                    await self._dispatch(frame)
        except (_Closed, WebSocketDisconnect):
            self._peer_gone = True
            return

    async def _handshake(self) -> bool:
        """Wait for a valid `auth` frame within auth_timeout_s of connecting (§6)."""
        deadline = time.monotonic() + self.settings.auth_timeout_s
        while True:
            remaining = deadline - time.monotonic()
            try:
                if remaining <= 0:
                    raise TimeoutError
                raw = await self._receive(remaining)
            except TimeoutError:
                self._log("auth.timeout", level=logging.WARNING)
                self.request_close(CLOSE_AUTH_TIMEOUT, "auth timeout")
                return False
            frame = self._parse(raw)
            if frame is None:
                if self._close_requested.is_set():
                    return False
                continue
            if not isinstance(frame, Auth):
                self.send_error(ErrorCode.UNAUTHENTICATED, "send auth first")
                continue
            try:
                ident = verify_token(frame.token, self.settings.jwt_secret)
            except AuthError as err:
                self._log("auth.rejected", level=logging.WARNING, code=err.code.value)
                self.send_error(err.code, err.message)
                if err.code is ErrorCode.TOKEN_EXPIRED:
                    continue  # refresh and re-authenticate on this socket, within the deadline
                self.request_close(CLOSE_POLICY, "unauthenticated")
                return False
            return await self._register(ident, frame.device_id)

    async def _register(self, ident: Identity, device_id: str) -> bool:
        info = ConnInfo(
            connection_id=self.id,
            user_id=ident.user_id,
            device_id=device_id,
            gateway_id=self.settings.gateway_id,
            connected_at=time.time(),
        )
        try:
            ok = await self.gw.register(self, info)
        except Exception:  # Redis down: without a registry entry no fan-out can reach us
            self._log("registry.error", level=logging.ERROR, op="register")
            self.send_error(ErrorCode.INTERNAL, "try again")
            self.request_close(CLOSE_INTERNAL, "registry unavailable")
            return False
        if not ok:
            self._log("auth.too_many_connections", level=logging.WARNING, user=ident.user_id)
            self.send_error(ErrorCode.RATE_LIMITED, "too many connections for this user")
            self.request_close(CLOSE_POLICY, "too many connections")
            return False
        self.info = info
        self.token_expires_at = ident.expires_at
        self._log("auth.ok", device_id=device_id)
        self.send(AuthOk(user_id=ident.user_id))
        return True

    async def _dispatch(self, frame: Any) -> None:
        # Any inbound frame proves liveness; pings are the regular heartbeat (§4, §6).
        if isinstance(frame, Ping):
            self.send(Pong())
        if isinstance(frame, Ping) or (
            time.monotonic() - self._last_refresh >= self.settings.heartbeat_interval_s
        ):
            await self._heartbeat()
        if isinstance(frame, Ping):
            return
        if isinstance(frame, Auth):
            await self._reauth(frame)
        elif self.token_expired_at is not None:
            cmid = frame.client_msg_id if isinstance(frame, MessageSend) else None
            self.send_error(ErrorCode.TOKEN_EXPIRED, "re-authenticate", client_msg_id=cmid)
        elif isinstance(frame, Subscribe):
            await self._subscribe(frame)
        elif isinstance(frame, MessageSend):
            await self._message_send(frame)
        elif isinstance(frame, Ack | Read):
            await self._ack(frame)

    async def _heartbeat(self) -> None:
        if self.info is None:
            return
        self._last_refresh = time.monotonic()
        try:
            await self.gw.registry.refresh(self.info)
        except Exception:
            self._log("registry.error", level=logging.WARNING, op="refresh")

    async def _reauth(self, frame: Auth) -> None:
        """Re-authentication on the same socket after TOKEN_EXPIRED (or proactively)."""
        try:
            ident = verify_token(frame.token, self.settings.jwt_secret)
        except AuthError as err:
            self._log("auth.rejected", level=logging.WARNING, code=err.code.value)
            self.send_error(err.code, err.message)
            if err.code is not ErrorCode.TOKEN_EXPIRED:
                self.request_close(CLOSE_POLICY, "unauthenticated")
            return
        if ident.user_id != self.user_id:
            self._log("auth.identity_changed", level=logging.WARNING)
            self.send_error(ErrorCode.UNAUTHENTICATED, "token is for a different user")
            self.request_close(CLOSE_POLICY, "unauthenticated")
            return
        self.token_expires_at = ident.expires_at
        self.token_expired_at = None
        self._log("auth.refreshed")
        self.send(AuthOk(user_id=ident.user_id))

    # ---- subscribe and replay (§2) -------------------------------------------------------

    async def _subscribe(self, frame: Subscribe) -> None:
        info = self._authed()
        try:
            membership = await self.gw.chat.check_member(frame.session_id, info.user_id)
        except ChatError as err:
            self._log("subscribe.failed", level=logging.WARNING, code=err.body.code.value)
            self.send_error(err.body.code, body=err.body)
            return
        if not membership.is_active:
            self._log("subscribe.not_member", level=logging.WARNING)
            self.send_error(ErrorCode.NOT_MEMBER, "not a member of this session")
            return
        if self._replay_task is not None:
            self._replay_task.cancel()
        self.session_id = frame.session_id
        self.phase = Phase.REPLAYING
        self.buffer = []
        self.buffer_bytes = 0
        self.replayed_through = max(0, frame.after_seq)
        self.recent.clear()
        self._replay_task = asyncio.create_task(
            self._replay(frame.session_id, self.replayed_through), name=f"{self.id}-replay"
        )

    async def _replay(self, session_id: str, after_seq: int) -> None:
        try:
            await self._replay_pages(session_id, after_seq)
        except asyncio.CancelledError:
            raise
        except ChatError as err:
            self._log("replay.failed", level=logging.WARNING, code=err.body.code.value)
            self._unsubscribe()
            self.send_error(err.body.code, body=err.body)
        except Exception as exc:  # never leave the socket stuck in REPLAYING
            self._log("conn.error", level=logging.ERROR, task="replay", exc_info=exc)
            self._unsubscribe()
            self.send_error(ErrorCode.INTERNAL, "sync failed; subscribe again")

    async def _replay_pages(self, session_id: str, after_seq: int) -> None:
        t0 = time.perf_counter()
        after = after_seq
        pages = 0
        while True:
            page = await self.gw.chat.get_messages(
                session_id, after_seq=after, limit=self.settings.replay_page_size
            )
            msgs = sorted((m for m in page.messages if m.seq > after), key=lambda m: m.seq)
            progressed = bool(msgs)
            if msgs:
                after = msgs[-1].seq
            done = not page.has_more or not progressed
            batch = Outgoing.from_model(
                SyncBatch(session_id=session_id, messages=msgs, has_more=not done)
            )
            # Replay pages wait for room instead of overflowing; live events keep buffering.
            await self.queue.wait_for_room(batch)
            pages += 1
            if not done:
                self.send(batch)
                continue
            # Last page: switch to live without yielding, so no live event can slip between.
            self.send(batch)
            self.replayed_through = after
            buffered, self.buffer, self.buffer_bytes = self.buffer, [], 0
            self.phase = Phase.LIVE
            for item in merge_buffered(buffered, after):
                if item.seq is not None:
                    self.recent.add(item.seq)
                self.send(item)
            self._log(
                "replay.done",
                seq=after,
                pages=pages,
                buffered=len(buffered),
                latency_ms=round((time.perf_counter() - t0) * 1000, 1),
            )
            return

    # ---- sends and acks ------------------------------------------------------------------

    async def _message_send(self, frame: MessageSend) -> None:
        info = self._authed()
        cmid = frame.client_msg_id
        if self.session_id is None:
            self.send_error(ErrorCode.VALIDATION, "subscribe first", client_msg_id=cmid)
            return
        trace = tracing.start_span(frame.trace_id)
        if len(frame.body) > MAX_BODY_CHARS:
            self._log("message.rejected", trace_id=trace.trace_id, code="MESSAGE_TOO_LONG")
            self.send_error(ErrorCode.MESSAGE_TOO_LONG, "message too long", client_msg_id=cmid)
            return
        # §8: identity comes from the token; client sender/seq/timestamps are never forwarded.
        req = SubmitMessageRequest(
            session_id=self.session_id,
            sender=Sender(type=SenderType.USER, id=info.user_id),
            client_msg_id=cmid,
            body=frame.body,
            trace_id=trace.traceparent,
        )
        t0 = time.perf_counter()
        try:
            resp = await self.gw.chat.submit_message(req, traceparent=trace.traceparent)
        except ChatError as err:
            self._log(
                "message.send_failed",
                level=logging.WARNING,
                trace_id=trace.trace_id,
                code=err.body.code.value,
                latency_ms=round((time.perf_counter() - t0) * 1000, 1),
            )
            self.send_error(err.body.code, body=err.body, client_msg_id=cmid)
            if err.body.code is ErrorCode.NOT_MEMBER:
                self._unsubscribe()
            return
        self._log(
            "message.sent",
            trace_id=trace.trace_id,
            message_id=resp.message_id,
            seq=resp.seq,
            duplicate=resp.duplicate or None,
            latency_ms=round((time.perf_counter() - t0) * 1000, 1),
        )
        self.send(MessageAck(client_msg_id=cmid, message_id=resp.message_id, seq=resp.seq))

    async def _ack(self, frame: Ack | Read) -> None:
        info = self._authed()
        if self.session_id is None:
            self.send_error(ErrorCode.VALIDATION, "subscribe first")
            return
        kind = "read" if isinstance(frame, Read) else "delivered"
        req = AckDeliveryRequest(
            participant_id=info.user_id,
            device_id=info.device_id,
            session_id=self.session_id,
            up_to_seq=frame.up_to_seq,
            kind=kind,
        )
        try:
            await self.gw.chat.ack(req)
        except ChatError as err:
            # Acks are cumulative: a lost one is recovered by the next (Deep Dive §7).
            self._log("ack.failed", level=logging.WARNING, code=err.body.code.value, kind=kind)
            if err.body.code is ErrorCode.NOT_MEMBER:
                self.send_error(ErrorCode.NOT_MEMBER, body=err.body)
                self._unsubscribe()

    def _unsubscribe(self) -> None:
        """§7: a removed member's socket is unsubscribed."""
        if self._replay_task is not None and self._replay_task is not asyncio.current_task():
            self._replay_task.cancel()
        self.session_id = None
        self.phase = Phase.IDLE
        self.buffer = []
        self.buffer_bytes = 0
