"""Gateway settings, read from the environment (names in deploy/.env.example).

Protocol timings default to Deep Dive §4/§6 values; they are fields so tests can shrink them.
"""

from __future__ import annotations

import os
import secrets
import socket
from collections.abc import Mapping
from dataclasses import dataclass

from huddle_contracts import redis_keys


class ConfigError(RuntimeError):
    """A required setting is missing or malformed."""


@dataclass(frozen=True, slots=True)
class Settings:
    gateway_id: str
    redis_url: str
    chat_url: str
    jwt_secret: str
    service_token: str
    host: str = "127.0.0.1"
    port: int = 8000

    # §6 connection protocol: the auth frame must arrive within 5 s.
    auth_timeout_s: float = 5.0
    # §4/§6 heartbeats: a ping every 20 s, three misses close the socket.
    heartbeat_interval_s: float = float(redis_keys.CONN_REFRESH_SECONDS)
    missed_heartbeats: int = 3
    # §4 registry: conn entries live 60 s, gw_alive 30 s.
    conn_ttl_s: float = float(redis_keys.CONN_TTL_SECONDS)
    gw_alive_ttl_s: float = float(redis_keys.GW_ALIVE_TTL_SECONDS)
    # §6/§8: at most 5 connections per user.
    max_conns_per_user: int = 5
    # §6 backpressure: 500 events or 1 MB per connection, then close 4008.
    queue_max_events: int = 500
    queue_max_bytes: int = 1024 * 1024
    # §2: replay from the store in pages of 200.
    replay_page_size: int = 200
    # Bounded wait for every internal HTTP call to the chat service.
    chat_timeout_s: float = 5.0
    # §2 draining: clients are told to reconnect after a random delay in [0, spread).
    drain_spread_ms: int = 10_000
    # How long a draining/closing socket gets to flush queued frames before it is closed.
    close_grace_s: float = 2.0
    # Largest inbound frame accepted (a 4,000-char body is at most 16 KB of UTF-8).
    max_frame_bytes: int = 64 * 1024
    # Resolution of the per-connection watchdog (heartbeat misses, token expiry).
    watchdog_tick_s: float = 1.0

    @property
    def heartbeat_timeout_s(self) -> float:
        return self.heartbeat_interval_s * self.missed_heartbeats

    @classmethod
    def from_env(cls, env: Mapping[str, str] | None = None) -> Settings:
        e = os.environ if env is None else env
        jwt_secret = e.get("HUDDLE_JWT_SECRET", "")
        service_token = e.get("HUDDLE_SERVICE_TOKEN", "")
        if not jwt_secret:
            raise ConfigError("HUDDLE_JWT_SECRET is required")
        if not service_token:
            raise ConfigError("HUDDLE_SERVICE_TOKEN is required")
        try:
            port = int(e.get("HUDDLE_GATEWAY_PORT", "8000"))
        except ValueError as exc:
            raise ConfigError("HUDDLE_GATEWAY_PORT must be an integer") from exc
        return cls(
            gateway_id=e.get("HUDDLE_GATEWAY_ID") or default_gateway_id(),
            redis_url=e.get("HUDDLE_REDIS_URL", "redis://127.0.0.1:6379/0"),
            chat_url=e.get("HUDDLE_CHAT_URL", "http://127.0.0.1:8001"),
            jwt_secret=jwt_secret,
            service_token=service_token,
            host=e.get("HUDDLE_GATEWAY_HOST", "127.0.0.1"),
            port=port,
        )


def default_gateway_id() -> str:
    """Unique per process, so replicas started without HUDDLE_GATEWAY_ID never share a channel."""
    return f"gw-{socket.gethostname()}-{secrets.token_hex(3)}"
