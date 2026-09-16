"""Nullable asynchronous completion fields; never infer historical completion."""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "1202_tracking"
down_revision = "1201_tracking"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "carrier_event_inbox", sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True)
    )
    op.add_column("carrier_event_inbox", sa.Column("result", postgresql.JSONB(), nullable=True))


def downgrade() -> None:
    op.drop_column("carrier_event_inbox", "result")
    op.drop_column("carrier_event_inbox", "completed_at")
