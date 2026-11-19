"""Persist Core identities, opaque sessions and access audit."""

from alembic import op
from sqlalchemy import (
    Boolean,
    CheckConstraint,
    Column,
    DateTime,
    ForeignKey,
    String,
    UniqueConstraint,
    Uuid,
)

revision = "2001_core"
down_revision = "1301_core"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "access_users",
        Column("id", Uuid, primary_key=True),
        Column("issuer", String(512), nullable=False),
        Column("subject", String(255), nullable=False),
        Column("display_name", String(255), nullable=False),
        Column("role", String(16), nullable=False),
        Column("enabled", Boolean, nullable=False),
        Column("created_at", DateTime(timezone=True), nullable=False),
        UniqueConstraint("issuer", "subject"),
        CheckConstraint("role IN ('viewer', 'operator')", name="role"),
    )
    op.create_table(
        "access_sessions",
        Column("token_hash", String(64), primary_key=True),
        Column("user_id", Uuid, ForeignKey("access_users.id", ondelete="RESTRICT"), nullable=False),
        Column("created_at", DateTime(timezone=True), nullable=False),
        Column("expires_at", DateTime(timezone=True), nullable=False),
        CheckConstraint("expires_at > created_at", name="expiry"),
    )
    op.create_table(
        "access_audit",
        Column("id", Uuid, primary_key=True),
        Column("user_id", Uuid, ForeignKey("access_users.id", ondelete="RESTRICT"), nullable=False),
        Column("executor", String(255), nullable=False),
        Column("action", String(32), nullable=False),
        Column("reason", String(1024), nullable=False),
        Column("occurred_at", DateTime(timezone=True), nullable=False),
        Column("old_role", String(16)),
        Column("new_role", String(16), nullable=False),
        Column("old_enabled", Boolean),
        Column("new_enabled", Boolean, nullable=False),
        CheckConstraint(
            "length(trim(executor)) > 0 AND length(trim(reason)) > 0", name="attribution"
        ),
    )
    op.create_index("ix_access_sessions_user_id", "access_sessions", ["user_id"])


def downgrade() -> None:
    op.drop_table("access_audit")
    op.drop_table("access_sessions")
    op.drop_table("access_users")
