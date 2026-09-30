"""Initial schema: the full Deep Dive §4 data model (expand only).

Revision ID: 0001
Revises:
Create Date: 2026-09-30

Tables: users, chat_sessions, session_memberships, agents, agent_runs (with the one-live-run
partial unique index), messages, session_summaries, agent_budgets (fixed window, D-019),
audit_events, refresh_tokens (rotating refresh tokens, Deep Dive §8).

agent_runs.output_message_id -> messages and messages.agent_run_id -> agent_runs reference each
other, so the first of the two foreign keys is added after both tables exist.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB, UUID

revision: str = "0001"
down_revision: str | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

TS = sa.DateTime(timezone=True)
NOW = sa.text("now()")
EMPTY_JSON = sa.text("'{}'::jsonb")
LIVE_RUN = "status IN ('eligible', 'context_built', 'generating', 'validated')"
RUN_STATUSES = (
    "status IN ('triggered', 'eligible', 'skipped', 'context_built', 'generating', "
    "'validated', 'committed', 'observed', 'cancelled', 'superseded', 'timed_out', 'failed')"
)


def upgrade() -> None:
    op.create_table(
        "users",
        sa.Column("id", sa.Text, nullable=False),
        sa.Column("display_name", sa.Text, nullable=False),
        sa.Column("created_at", TS, nullable=False, server_default=NOW),
        sa.PrimaryKeyConstraint("id", name="pk_users"),
    )
    op.create_table(
        "chat_sessions",
        sa.Column("id", sa.Text, nullable=False),
        sa.Column("title", sa.Text, nullable=False),
        sa.Column("status", sa.Text, nullable=False, server_default="active"),
        sa.Column("last_seq", sa.BigInteger, nullable=False, server_default="0"),
        sa.Column("last_human_message_at", TS, nullable=True),
        sa.Column("created_by", sa.Text, nullable=True),
        sa.Column("created_at", TS, nullable=False, server_default=NOW),
        sa.Column("closed_at", TS, nullable=True),
        sa.PrimaryKeyConstraint("id", name="pk_chat_sessions"),
        sa.ForeignKeyConstraint(
            ["created_by"], ["users.id"], name="fk_chat_sessions_created_by_users"
        ),
        sa.CheckConstraint("status IN ('active', 'closed')", name="ck_chat_sessions_status"),
    )
    op.create_table(
        "session_memberships",
        sa.Column("session_id", sa.Text, nullable=False),
        sa.Column("participant_id", sa.Text, nullable=False),
        sa.Column("participant_type", sa.Text, nullable=False),
        sa.Column("role", sa.Text, nullable=False, server_default="member"),
        sa.Column("status", sa.Text, nullable=False, server_default="active"),
        sa.Column("last_delivered_seq", sa.BigInteger, nullable=False, server_default="0"),
        sa.Column("last_read_seq", sa.BigInteger, nullable=False, server_default="0"),
        sa.Column("preferences", JSONB, nullable=False, server_default=EMPTY_JSON),
        sa.Column("joined_at", TS, nullable=False, server_default=NOW),
        sa.Column("left_at", TS, nullable=True),
        sa.PrimaryKeyConstraint("session_id", "participant_id", name="pk_session_memberships"),
        sa.ForeignKeyConstraint(
            ["session_id"],
            ["chat_sessions.id"],
            name="fk_session_memberships_session_id_chat_sessions",
        ),
        sa.CheckConstraint(
            "participant_type IN ('user', 'agent')",
            name="ck_session_memberships_participant_type",
        ),
        sa.CheckConstraint("role IN ('owner', 'member')", name="ck_session_memberships_role"),
        sa.CheckConstraint(
            "status IN ('invited', 'active', 'left', 'removed')",
            name="ck_session_memberships_status",
        ),
    )
    op.create_index(
        "ix_session_memberships_participant_id", "session_memberships", ["participant_id"]
    )
    op.create_table(
        "agents",
        sa.Column("id", sa.Text, nullable=False),
        sa.Column("name", sa.Text, nullable=False),
        sa.Column("avatar_url", sa.Text, nullable=False, server_default=""),
        sa.Column("model", sa.Text, nullable=False),
        sa.Column("fallback_model", sa.Text, nullable=False, server_default=""),
        sa.Column("prompt_version", sa.Text, nullable=False),
        sa.Column("config", JSONB, nullable=False, server_default=EMPTY_JSON),
        sa.Column("status", sa.Text, nullable=False, server_default="active"),
        sa.Column("created_at", TS, nullable=False, server_default=NOW),
        sa.Column("updated_at", TS, nullable=False, server_default=NOW),
        sa.PrimaryKeyConstraint("id", name="pk_agents"),
        sa.CheckConstraint("status IN ('active', 'paused', 'removed')", name="ck_agents_status"),
    )
    op.create_table(
        "agent_runs",
        sa.Column("id", sa.Text, nullable=False),
        sa.Column("session_id", sa.Text, nullable=False),
        sa.Column("agent_id", sa.Text, nullable=False),
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
        sa.Column("output_message_id", sa.Text, nullable=True),
        sa.Column("created_at", TS, nullable=False, server_default=NOW),
        sa.Column("updated_at", TS, nullable=False, server_default=NOW),
        sa.PrimaryKeyConstraint("id", name="pk_agent_runs"),
        sa.ForeignKeyConstraint(
            ["session_id"], ["chat_sessions.id"], name="fk_agent_runs_session_id_chat_sessions"
        ),
        sa.ForeignKeyConstraint(["agent_id"], ["agents.id"], name="fk_agent_runs_agent_id_agents"),
        sa.CheckConstraint(
            "trigger_kind IN ('message', 'nudge')", name="ck_agent_runs_trigger_kind"
        ),
        sa.CheckConstraint(RUN_STATUSES, name="ck_agent_runs_status"),
    )
    # Deep Dive §4/§5: only one live run per (session, agent).
    op.create_index(
        "uq_agent_runs_one_live_run",
        "agent_runs",
        ["session_id", "agent_id"],
        unique=True,
        postgresql_where=sa.text(LIVE_RUN),
    )
    op.create_table(
        "messages",
        sa.Column("id", sa.Text, nullable=False),
        sa.Column("session_id", sa.Text, nullable=False),
        sa.Column("seq", sa.BigInteger, nullable=False),
        sa.Column("sender_type", sa.Text, nullable=False),
        sa.Column("sender_id", sa.Text, nullable=False),
        sa.Column("client_msg_id", UUID(as_uuid=False), nullable=False),
        sa.Column("kind", sa.Text, nullable=False, server_default="text"),
        sa.Column("body", sa.Text, nullable=True),
        sa.Column("agent_run_id", sa.Text, nullable=True),
        sa.Column("created_at", TS, nullable=False, server_default=NOW),
        sa.Column("redacted_at", TS, nullable=True),
        sa.PrimaryKeyConstraint("id", name="pk_messages"),
        sa.ForeignKeyConstraint(
            ["session_id"], ["chat_sessions.id"], name="fk_messages_session_id_chat_sessions"
        ),
        sa.ForeignKeyConstraint(
            ["agent_run_id"], ["agent_runs.id"], name="fk_messages_agent_run_id_agent_runs"
        ),
        # Gapless ordering backstop; also serves replay and window queries (Deep Dive §2, §4).
        sa.UniqueConstraint("session_id", "seq", name="uq_messages_session_id_seq"),
        # Idempotency key (Deep Dive §2).
        sa.UniqueConstraint(
            "session_id",
            "sender_id",
            "client_msg_id",
            name="uq_messages_session_id_sender_id_client_msg_id",
        ),
        sa.CheckConstraint(
            "sender_type IN ('user', 'agent', 'system')", name="ck_messages_sender_type"
        ),
        sa.CheckConstraint("kind IN ('text', 'system')", name="ck_messages_kind"),
        sa.CheckConstraint(
            "body IS NULL OR char_length(body) <= 4000", name="ck_messages_body_length"
        ),
    )
    op.create_foreign_key(
        "fk_agent_runs_output_message_id_messages",
        "agent_runs",
        "messages",
        ["output_message_id"],
        ["id"],
    )
    op.create_table(
        "session_summaries",
        sa.Column("id", sa.BigInteger, sa.Identity(always=False), nullable=False),
        sa.Column("session_id", sa.Text, nullable=False),
        sa.Column("agent_id", sa.Text, nullable=False),
        sa.Column("covers_through_seq", sa.BigInteger, nullable=False),
        sa.Column("summary", sa.Text, nullable=False),
        sa.Column("token_count", sa.Integer, nullable=False),
        sa.Column("model", sa.Text, nullable=False),
        sa.Column("created_at", TS, nullable=False, server_default=NOW),
        sa.PrimaryKeyConstraint("id", name="pk_session_summaries"),
        sa.ForeignKeyConstraint(
            ["session_id"],
            ["chat_sessions.id"],
            name="fk_session_summaries_session_id_chat_sessions",
        ),
        sa.ForeignKeyConstraint(
            ["agent_id"], ["agents.id"], name="fk_session_summaries_agent_id_agents"
        ),
    )
    op.create_index(
        "ix_session_summaries_latest",
        "session_summaries",
        ["session_id", "agent_id", sa.text("created_at DESC")],
    )
    op.create_table(
        "agent_budgets",
        sa.Column("session_id", sa.Text, nullable=False),
        sa.Column("agent_id", sa.Text, nullable=False),
        sa.Column("capacity_tokens", sa.BigInteger, nullable=False),
        sa.Column("used_tokens", sa.BigInteger, nullable=False, server_default="0"),
        sa.Column("window_seconds", sa.Integer, nullable=False),
        sa.Column("window_started_at", TS, nullable=False),
        sa.Column("updated_at", TS, nullable=False, server_default=NOW),
        sa.PrimaryKeyConstraint("session_id", "agent_id", name="pk_agent_budgets"),
        sa.ForeignKeyConstraint(
            ["session_id"], ["chat_sessions.id"], name="fk_agent_budgets_session_id_chat_sessions"
        ),
        sa.ForeignKeyConstraint(
            ["agent_id"], ["agents.id"], name="fk_agent_budgets_agent_id_agents"
        ),
        sa.CheckConstraint("capacity_tokens >= 0", name="ck_agent_budgets_capacity_nonneg"),
        sa.CheckConstraint("used_tokens >= 0", name="ck_agent_budgets_used_nonneg"),
        sa.CheckConstraint("window_seconds > 0", name="ck_agent_budgets_window_positive"),
    )
    op.create_table(
        "audit_events",
        sa.Column("id", sa.BigInteger, sa.Identity(always=False), nullable=False),
        sa.Column("session_id", sa.Text, nullable=True),
        sa.Column("actor_id", sa.Text, nullable=False),
        sa.Column("action", sa.Text, nullable=False),
        sa.Column("target", sa.Text, nullable=True),
        sa.Column("payload", JSONB, nullable=False, server_default=EMPTY_JSON),
        sa.Column("created_at", TS, nullable=False, server_default=NOW),
        sa.PrimaryKeyConstraint("id", name="pk_audit_events"),
        sa.ForeignKeyConstraint(
            ["session_id"], ["chat_sessions.id"], name="fk_audit_events_session_id_chat_sessions"
        ),
    )
    op.create_index("ix_audit_events_session_created", "audit_events", ["session_id", "created_at"])
    op.create_table(
        "refresh_tokens",
        sa.Column("id", sa.Text, nullable=False),
        sa.Column("user_id", sa.Text, nullable=False),
        sa.Column("family_id", sa.Text, nullable=False),
        sa.Column("token_hash", sa.Text, nullable=False),
        sa.Column("created_at", TS, nullable=False, server_default=NOW),
        sa.Column("expires_at", TS, nullable=False),
        sa.Column("used_at", TS, nullable=True),
        sa.Column("revoked_at", TS, nullable=True),
        sa.PrimaryKeyConstraint("id", name="pk_refresh_tokens"),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], name="fk_refresh_tokens_user_id_users"),
        sa.UniqueConstraint("token_hash", name="uq_refresh_tokens_token_hash"),
    )
    op.create_index("ix_refresh_tokens_user_id", "refresh_tokens", ["user_id"])
    op.create_index("ix_refresh_tokens_family_id", "refresh_tokens", ["family_id"])


def downgrade() -> None:
    op.drop_table("refresh_tokens")
    op.drop_table("audit_events")
    op.drop_table("agent_budgets")
    op.drop_table("session_summaries")
    op.drop_constraint("fk_agent_runs_output_message_id_messages", "agent_runs")
    op.drop_table("messages")
    op.drop_table("agent_runs")
    op.drop_table("agents")
    op.drop_table("session_memberships")
    op.drop_table("chat_sessions")
    op.drop_table("users")
