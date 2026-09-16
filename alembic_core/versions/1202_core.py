"""Audited transport rearm and last local attempt, owned by core."""

import sqlalchemy as sa
from alembic import op

revision = "1202_core"
down_revision = "1201_core"
branch_labels = None
depends_on = None


def upgrade() -> None:
    for name in ("message_inbox", "message_outbox"):
        op.add_column(name, sa.Column("last_attempt_at", sa.DateTime(timezone=True), nullable=True))
    op.create_table(
        "message_rearm",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("stage", sa.String(16), nullable=False),
        sa.Column("message_id", sa.Uuid(), nullable=False),
        sa.Column("generation", sa.Integer(), nullable=False),
        sa.Column("body_sha256", sa.CHAR(64), nullable=False),
        sa.Column("previous_attempts", sa.Integer(), nullable=False),
        sa.Column("previous_reason", sa.String(64)),
        sa.Column("reason", sa.String(240), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint(
            "stage", "message_id", "generation", name="uq_message_rearm_generation"
        ),
        sa.CheckConstraint("stage IN ('inbox', 'outbox')", name=op.f("ck_message_rearm_stage")),
        sa.CheckConstraint(
            "generation > 0 AND previous_attempts BETWEEN 0 AND 5",
            name=op.f("ck_message_rearm_attempts"),
        ),
        sa.CheckConstraint(
            "body_sha256 ~ '^[0-9a-f]{64}$'", name=op.f("ck_message_rearm_body_hash")
        ),
        sa.CheckConstraint("btrim(reason) <> ''", name=op.f("ck_message_rearm_reason")),
    )


def downgrade() -> None:
    op.drop_table("message_rearm")
    for name in ("message_inbox", "message_outbox"):
        op.drop_column(name, "last_attempt_at")
