"""Prefixed ULID identifiers (Deep Dive §1, §4): usr_, ses_, msg_, agt_, evt_, rt_."""

from __future__ import annotations

from ulid import ULID


def new_id(prefix: str) -> str:
    return f"{prefix}_{ULID()}"
