"""Persist one-use OIDC login state in Core."""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB

revision = "2002_core"
down_revision = "2001_core"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "access_login_flows",
        sa.Column("token_hash", sa.String(64), primary_key=True),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("state", JSONB, nullable=False),
    )


def downgrade() -> None:
    op.drop_table("access_login_flows")
