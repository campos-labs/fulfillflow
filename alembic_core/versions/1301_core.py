"""Permit the Notifications event in the Core outbox without activating its writer."""

from alembic import op

revision = "1301_core"
down_revision = "1202_core"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.drop_constraint(op.f("ck_message_outbox_type"), "message_outbox", type_="check")
    op.create_check_constraint(
        op.f("ck_message_outbox_type"),
        "message_outbox",
        "type IN ('tracking.apply.v1', 'tracking.result.v1', 'shipment.status_changed.v1')",
    )
    op.create_index(
        "ix_message_outbox_flow_pending",
        "message_outbox",
        ["type", "state", "next_attempt_at"],
    )


def downgrade() -> None:
    # PostgreSQL refuses this downgrade while new-flow rows still exist.
    op.drop_constraint(op.f("ck_message_outbox_type"), "message_outbox", type_="check")
    op.create_check_constraint(
        op.f("ck_message_outbox_type"),
        "message_outbox",
        "type IN ('tracking.apply.v1', 'tracking.result.v1')",
    )
    op.drop_index("ix_message_outbox_flow_pending", table_name="message_outbox")
