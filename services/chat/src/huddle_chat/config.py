"""Chat service settings, read from the environment (names in deploy/.env.example)."""

from __future__ import annotations

import os
from collections.abc import Mapping
from dataclasses import dataclass

from huddle_contracts.budget import DEFAULT_CAPACITY_TOKENS, DEFAULT_WINDOW_SECONDS

DEFAULT_SESSION_ID = "ses_main"
DEFAULT_CORS_ORIGINS = ("http://localhost:5173", "http://127.0.0.1:5173")  # Vite dev server


class ConfigError(RuntimeError):
    pass


def to_async_dsn(dsn: str) -> str:
    """Accept a plain libpq-style DSN and point SQLAlchemy at the asyncpg driver."""
    for prefix in ("postgresql+asyncpg://", "postgresql://", "postgres://"):
        if dsn.startswith(prefix):
            return "postgresql+asyncpg://" + dsn[len(prefix) :]
    raise ConfigError("HUDDLE_POSTGRES_DSN must be a postgresql:// URL")


@dataclass(frozen=True)
class Settings:
    postgres_dsn: str
    redis_url: str
    jwt_secret: str
    service_token: str
    session_id: str = DEFAULT_SESSION_ID
    session_title: str = "Huddle"
    agent_name: str = "Huddle AI"
    agent_model: str = "llama3.1:8b"
    agent_fallback_model: str = ""
    agent_prompt_version: str = "v1"
    budget_capacity_tokens: int = DEFAULT_CAPACITY_TOKENS
    budget_window_seconds: int = DEFAULT_WINDOW_SECONDS
    cors_origins: tuple[str, ...] = DEFAULT_CORS_ORIGINS
    access_ttl_seconds: int = 15 * 60  # Deep Dive §8: 15 min JWT
    refresh_ttl_seconds: int = 30 * 24 * 3600  # Deep Dive §8: 30 days, rotates on use
    rate_limit_messages: int = 10  # Deep Dive §6: 10 messages per 10 s per user per session
    rate_limit_window_ms: int = 10_000
    migrate_on_startup: bool = True
    # The container listens on its private network; the host is configurable.
    host: str = "0.0.0.0"  # noqa: S104  # nosec B104
    port: int = 8001

    @property
    def async_dsn(self) -> str:
        return to_async_dsn(self.postgres_dsn)

    @classmethod
    def from_env(cls, env: Mapping[str, str] | None = None) -> Settings:
        e = os.environ if env is None else env

        def required(name: str) -> str:
            value = e.get(name, "").strip()
            if not value:
                raise ConfigError(f"{name} is required")
            return value

        def integer(name: str, default: int) -> int:
            raw = e.get(name, "").strip()
            if not raw:
                return default
            try:
                return int(raw)
            except ValueError as exc:
                raise ConfigError(f"{name} must be an integer") from exc

        cors_raw = e.get("HUDDLE_CORS_ORIGINS")
        cors = (
            DEFAULT_CORS_ORIGINS
            if cors_raw is None
            else tuple(o.strip() for o in cors_raw.split(",") if o.strip())
        )
        settings = cls(
            postgres_dsn=required("HUDDLE_POSTGRES_DSN"),
            redis_url=required("HUDDLE_REDIS_URL"),
            jwt_secret=required("HUDDLE_JWT_SECRET"),
            service_token=required("HUDDLE_SERVICE_TOKEN"),
            session_id=e.get("HUDDLE_SESSION_ID", "").strip() or DEFAULT_SESSION_ID,
            session_title=e.get("HUDDLE_SESSION_TITLE", "").strip() or "Huddle",
            agent_model=e.get("HUDDLE_WRITER_MODEL", "").strip() or "llama3.1:8b",
            agent_fallback_model=e.get("HUDDLE_FALLBACK_MODEL", "").strip(),
            budget_capacity_tokens=integer(
                "HUDDLE_BUDGET_CAPACITY_TOKENS", DEFAULT_CAPACITY_TOKENS
            ),
            budget_window_seconds=integer("HUDDLE_BUDGET_WINDOW_SECONDS", DEFAULT_WINDOW_SECONDS),
            cors_origins=cors,
            migrate_on_startup=e.get("HUDDLE_CHAT_MIGRATE_ON_STARTUP", "1").strip() != "0",
            host=e.get("HUDDLE_CHAT_HOST", "").strip() or cls.host,
            port=integer("HUDDLE_CHAT_PORT", 8001),
        )
        to_async_dsn(settings.postgres_dsn)
        if settings.budget_window_seconds <= 0 or settings.budget_capacity_tokens < 0:
            raise ConfigError("budget capacity must be >= 0 and window > 0")
        return settings
