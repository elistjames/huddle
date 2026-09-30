"""Per-connection outbound queue and replay/live merging (Deep Dive §2, §6 backpressure)."""

from __future__ import annotations

import asyncio
import json
from collections import deque
from collections.abc import Iterable
from dataclasses import dataclass
from typing import Any, cast

from pydantic import BaseModel


@dataclass(frozen=True, slots=True)
class Outgoing:
    """One serialized server frame. `frame` is kept for routing decisions (type, session, seq)."""

    text: str
    nbytes: int
    type: str
    session_id: str | None = None
    seq: int | None = None

    @classmethod
    def from_model(cls, model: BaseModel) -> Outgoing:
        return cls.from_text(model.model_dump_json(), model.model_dump(include={"type"}))

    @classmethod
    def from_frame(cls, frame: dict[str, Any]) -> Outgoing:
        return cls.from_text(json.dumps(frame, separators=(",", ":"), ensure_ascii=False), frame)

    @classmethod
    def from_text(cls, text: str, frame: dict[str, Any]) -> Outgoing:
        ftype = frame.get("type")
        session = frame.get("session_id")
        return cls(
            text=text,
            nbytes=len(text.encode()),
            type=ftype if isinstance(ftype, str) else "",
            session_id=session if isinstance(session, str) else None,
            seq=_frame_seq(frame),
        )


def _frame_seq(frame: dict[str, Any]) -> int | None:
    if frame.get("type") != "message.created":
        return None
    seq = frame.get("sequence")
    if isinstance(seq, int) and not isinstance(seq, bool):
        return seq
    message = frame.get("message")
    if isinstance(message, dict):
        inner = cast("dict[str, Any]", message).get("seq")
        if isinstance(inner, int) and not isinstance(inner, bool):
            return inner
    return None


class OutboundQueue:
    """FIFO bounded by event count and bytes. `put` never blocks: it reports overflow instead,
    so the caller can close a slow consumer with 4008 rather than buffer without limit."""

    def __init__(self, *, max_events: int, max_bytes: int) -> None:
        self.max_events = max_events
        self.max_bytes = max_bytes
        self._items: deque[Outgoing] = deque()
        self._bytes = 0
        self._in_flight = 0
        self._changed = asyncio.Event()

    def __len__(self) -> int:
        return len(self._items)

    @property
    def nbytes(self) -> int:
        return self._bytes

    def has_room(self, item: Outgoing) -> bool:
        if not self._items:
            return True  # a single frame always fits in an empty queue
        return len(self._items) < self.max_events and self._bytes + item.nbytes <= self.max_bytes

    def put(self, item: Outgoing) -> bool:
        if not self.has_room(item):
            return False
        self._items.append(item)
        self._bytes += item.nbytes
        self._changed.set()
        return True

    async def wait_for_room(self, item: Outgoing) -> None:
        while not self.has_room(item):
            self._changed.clear()
            await self._changed.wait()

    async def get(self) -> Outgoing:
        while not self._items:
            self._changed.clear()
            await self._changed.wait()
        item = self._items.popleft()
        self._bytes -= item.nbytes
        self._in_flight += 1
        self._changed.set()
        return item

    def task_done(self) -> None:
        self._in_flight -= 1
        self._changed.set()

    async def wait_empty(self) -> None:
        while self._items or self._in_flight:
            self._changed.clear()
            await self._changed.wait()


class SeqWindow:
    """Recently delivered `seq`s, to drop pub/sub duplicates without dropping late arrivals."""

    def __init__(self, size: int = 1024) -> None:
        self._order: deque[int] = deque()
        self._seen: set[int] = set()
        self._size = size

    def __contains__(self, seq: int) -> bool:
        return seq in self._seen

    def add(self, seq: int) -> None:
        if seq in self._seen:
            return
        self._order.append(seq)
        self._seen.add(seq)
        if len(self._order) > self._size:
            self._seen.discard(self._order.popleft())

    def clear(self) -> None:
        self._order.clear()
        self._seen.clear()


def merge_buffered(buffered: Iterable[Outgoing], replayed_through: int) -> list[Outgoing]:
    """Merge events buffered during replay (Deep Dive §2).

    `message.created` frames already covered by the replay (seq <= replayed_through) or repeated
    are dropped; the rest come out in seq order, followed by the other frames in arrival order.
    """
    messages: dict[int, Outgoing] = {}
    others: list[Outgoing] = []
    for item in buffered:
        if item.type == "message.created" and item.seq is not None:
            if item.seq > replayed_through and item.seq not in messages:
                messages[item.seq] = item
        else:
            others.append(item)
    return [messages[s] for s in sorted(messages)] + others
