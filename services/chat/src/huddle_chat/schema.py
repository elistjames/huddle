"""Postgres schema (Deep Dive §4) as SQLAlchemy Core tables.

The chat service owns every migration for this schema (docs/work/README.md); the agent service
reads messages/memberships/agents/chat_sessions and writes agent_runs/session_summaries with its
own credentials. Keep this module and `migrations/versions/` in lockstep: the integration test
`test_chat_migrations.py` fails if they drift.
"""

from __future__ import annotations

import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSONB, UUID

metadata = sa.MetaData(
    naming_convention={
        "ix": "ix_%(table_name)s_%(column_0_N_name)s",
        "uq": "uq_%(table_name)s_%(column_0_N_name)s",
        "ck": "ck_%(table_name)s_%(constraint_name)s",
        "fk": "fk_%(table_name)s_%(column_0_name)s_%(referred_table_name)s",
        "pk": "pk_%(table_name)s",
    }
)

TS = sa.DateTime(timezone=True)
NOW = sa.text("now()")
EMPTY_JSON = sa.text("'{}'::jsonb")

# Agent run statuses (Deep Dive §1, §5). "Live" statuses are covered by the partial unique index.
RUN_STATUSES = (
    "triggered",
    "eligible",
    "skipped",
    "context_built",
    "generating",
    "validated",
    "committed",
    "observed",
    "cancelled",
    "superseded",
    "timed_out",
    "failed",
)
LIVE_RUN_STATUSES = ("eligible", "context_built", "generating", "validated")


def _in(column: str, values: tuple[str, ...]) -> str:
    return f"{column} IN ({', '.join(repr(v) for v in values)})"


users = sa.Table(
    "users",
    metadata,
    sa.Column("id", sa.Text, primary_key=True),
    sa.Column("display_name", sa.Text, nullable=False),
    sa.Column("created_at", TS, nullable=False, server_default=NOW),
)

chat_sessions = sa.Table(
    "chat_sessions",
    metadata,
    sa.Column("id", sa.Text, primary_key=True),
    sa.Column("title", sa.Text, nullable=False),
    sa.Column("status", sa.Text, nullable=False, server_default="active"),
    sa.Column("last_seq", sa.BigInteger, nullable=False, server_default="0"),
    sa.Column("last_human_message_at", TS, nullable=True),
    sa.Column("created_by", sa.Text, sa.ForeignKey("users.id"), nullable=True),
    sa.Column("created_at", TS, nullable=False, server_default=NOW),
    sa.Column("closed_at", TS, nullable=True),
    sa.CheckConstraint(_in("status", ("active", "closed")), name="status"),
)

session_memberships = sa.Table(
    "session_memberships",
    metadata,
    sa.Column("session_id", sa.Text, sa.ForeignKey("chat_sessions.id"), primary_key=True),
    sa.Column("participant_id", sa.Text, primary_key=True, index=True),
    sa.Column("participant_type", sa.Text, nullable=False),
    sa.Column("role", sa.Text, nullable=False, server_default="member"),
    sa.Column("status", sa.Text, nullable=False, server_default="active"),
    sa.Column("last_delivered_seq", sa.BigInteger, nullable=False, server_default="0"),
    sa.Column("last_read_seq", sa.BigInteger, nullable=False, server_default="0"),
    sa.Column("preferences", JSONB, nullable=False, server_default=EMPTY_JSON),
    sa.Column("joined_at", TS, nullable=False, server_default=NOW),
    sa.Column("left_at", TS, nullable=True),
    sa.CheckConstraint(_in("participant_type", ("user", "agent")), name="participant_type"),
    sa.CheckConstraint(_in("role", ("owner", "member")), name="role"),
    sa.CheckConstraint(_in("status", ("invited", "active", "left", "removed")), name="status"),
)

agents = sa.Table(
    "agents",
    metadata,
    sa.Column("id", sa.Text, primary_key=True),
    sa.Column("name", sa.Text, nullable=False),
    sa.Column("avatar_url", sa.Text, nullable=False, server_default=""),
    sa.Column("model", sa.Text, nullable=False),
    sa.Column("fallback_model", sa.Text, nullable=False, server_default=""),
    sa.Column("prompt_version", sa.Text, nullable=False),
    sa.Column("config", JSONB, nullable=False, server_default=EMPTY_JSON),
    sa.Column("status", sa.Text, nullable=False, server_default="active"),
    sa.Column("created_at", TS, nullable=False, server_default=NOW),
    sa.Column("updated_at", TS, nullable=False, server_default=NOW),
    sa.CheckConstraint(_in("status", ("active", "paused", "removed")), name="status"),
)

agent_runs = sa.Table(
    "agent_runs",
    metadata,
    sa.Column("id", sa.Text, primary_key=True),
    sa.Column("session_id", sa.Text, sa.ForeignKey("chat_sessions.id"), nullable=False),
    sa.Column("agent_id", sa.Text, sa.ForeignKey("agents.id"), nullable=False),
    sa.Column("trigger_kind", sa.Text, nullable=False),
    sa.Column("trigger_seq", sa.BigInteger, nullable=False),
    sa.Column("context_seq", sa.BigInteger, nullable=False),
    sa.Column("status", sa.Text, nullable=False),
    sa.Column("skip_reason", sa.Text, nullable=True),
    sa.Column("error_code", sa.Text, nullable=True),
    sa.Column("lease_owner", sa.Text, nullable=True),
    sa.Column("lease_expires_at", TS, nullable=True),
    sa.Column("attempts", sa.Integer, nullable=True, server_default="0"),
    sa.Column("prompt_version", sa.Text, nullable=True),
    sa.Column("model", sa.Text, nullable=True),
    sa.Column("input_tokens", sa.Integer, nullable=True),
    sa.Column("output_tokens", sa.Integer, nullable=True),
    sa.Column("latency_ms", sa.Integer, nullable=True),
    sa.Column(
        "output_message_id",
        sa.Text,
        sa.ForeignKey("messages.id", use_alter=True),
        nullable=True,
    ),
    sa.Column("created_at", TS, nullable=False, server_default=NOW),
    sa.Column("updated_at", TS, nullable=False, server_default=NOW),
    sa.CheckConstraint(_in("trigger_kind", ("message", "nudge")), name="trigger_kind"),
    sa.CheckConstraint(_in("status", RUN_STATUSES), name="status"),
    sa.Index(
        "uq_agent_runs_one_live_run",
        "session_id",
        "agent_id",
        unique=True,
        postgresql_where=sa.text(_in("status", LIVE_RUN_STATUSES)),
    ),
)

messages = sa.Table(
    "messages",
    metadata,
    sa.Column("id", sa.Text, primary_key=True),
    sa.Column("session_id", sa.Text, sa.ForeignKey("chat_sessions.id"), nullable=False),
    sa.Column("seq", sa.BigInteger, nullable=False),
    sa.Column("sender_type", sa.Text, nullable=False),
    sa.Column("sender_id", sa.Text, nullable=False),
    sa.Column("client_msg_id", UUID(as_uuid=False), nullable=False),
    sa.Column("kind", sa.Text, nullable=False, server_default="text"),
    sa.Column("body", sa.Text, nullable=True),
    sa.Column("agent_run_id", sa.Text, sa.ForeignKey("agent_runs.id"), nullable=True),
    sa.Column("created_at", TS, nullable=False, server_default=NOW),
    sa.Column("redacted_at", TS, nullable=True),
    sa.UniqueConstraint("session_id", "seq"),
    sa.UniqueConstraint("session_id", "sender_id", "client_msg_id"),
    sa.CheckConstraint(_in("sender_type", ("user", "agent", "system")), name="sender_type"),
    sa.CheckConstraint(_in("kind", ("text", "system")), name="kind"),
    sa.CheckConstraint("body IS NULL OR char_length(body) <= 4000", name="body_length"),
)

session_summaries = sa.Table(
    "session_summaries",
    metadata,
    sa.Column("id", sa.BigInteger, sa.Identity(always=False), primary_key=True),
    sa.Column("session_id", sa.Text, sa.ForeignKey("chat_sessions.id"), nullable=False),
    sa.Column("agent_id", sa.Text, sa.ForeignKey("agents.id"), nullable=False),
    sa.Column("covers_through_seq", sa.BigInteger, nullable=False),
    sa.Column("summary", sa.Text, nullable=False),
    sa.Column("token_count", sa.Integer, nullable=False),
    sa.Column("model", sa.Text, nullable=False),
    sa.Column("created_at", TS, nullable=False, server_default=NOW),
    sa.Index(
        "ix_session_summaries_latest",
        "session_id",
        "agent_id",
        sa.text("created_at DESC"),
    ),
)

agent_budgets = sa.Table(
    "agent_budgets",
    metadata,
    sa.Column("session_id", sa.Text, sa.ForeignKey("chat_sessions.id"), primary_key=True),
    sa.Column("agent_id", sa.Text, sa.ForeignKey("agents.id"), primary_key=True),
    sa.Column("capacity_tokens", sa.BigInteger, nullable=False),
    sa.Column("used_tokens", sa.BigInteger, nullable=False, server_default="0"),
    sa.Column("window_seconds", sa.Integer, nullable=False),
    sa.Column("window_started_at", TS, nullable=False),
    sa.Column("updated_at", TS, nullable=False, server_default=NOW),
    sa.CheckConstraint("capacity_tokens >= 0", name="capacity_nonneg"),
    sa.CheckConstraint("used_tokens >= 0", name="used_nonneg"),
    sa.CheckConstraint("window_seconds > 0", name="window_positive"),
)

audit_events = sa.Table(
    "audit_events",
    metadata,
    sa.Column("id", sa.BigInteger, sa.Identity(always=False), primary_key=True),
    sa.Column("session_id", sa.Text, sa.ForeignKey("chat_sessions.id"), nullable=True),
    sa.Column("actor_id", sa.Text, nullable=False),
    sa.Column("action", sa.Text, nullable=False),
    sa.Column("target", sa.Text, nullable=True),
    sa.Column("payload", JSONB, nullable=False, server_default=EMPTY_JSON),
    sa.Column("created_at", TS, nullable=False, server_default=NOW),
    sa.Index("ix_audit_events_session_created", "session_id", "created_at"),
)

# Rotating refresh tokens (Deep Dive §8). Only a SHA-256 of the opaque token is stored.
refresh_tokens = sa.Table(
    "refresh_tokens",
    metadata,
    sa.Column("id", sa.Text, primary_key=True),
    sa.Column("user_id", sa.Text, sa.ForeignKey("users.id"), nullable=False, index=True),
    sa.Column("family_id", sa.Text, nullable=False, index=True),
    sa.Column("token_hash", sa.Text, nullable=False, unique=True),
    sa.Column("created_at", TS, nullable=False, server_default=NOW),
    sa.Column("expires_at", TS, nullable=False),
    sa.Column("used_at", TS, nullable=True),
    sa.Column("revoked_at", TS, nullable=True),
)
