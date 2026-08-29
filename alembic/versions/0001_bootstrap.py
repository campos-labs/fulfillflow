"""Establish the schema baseline used by the startup compatibility gate.

Revision ID: 0001_bootstrap
Revises:
Create Date: 2026-08-29
"""

from collections.abc import Sequence

revision: str = "0001_bootstrap"
down_revision: str | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Record the bootstrap head without introducing domain tables."""


def downgrade() -> None:
    """Remove the bootstrap head without dropping application data."""
