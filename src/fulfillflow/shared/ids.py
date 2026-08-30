"""Application-owned identifier generation."""

from uuid import UUID, uuid4


def new_uuid() -> UUID:
    """Generate one native UUIDv4 identifier."""
    return uuid4()
