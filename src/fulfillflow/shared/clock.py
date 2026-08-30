"""Injectable domain clock."""

from datetime import UTC, datetime
from typing import Protocol


class Clock(Protocol):
    """Supply timezone-aware UTC instants to application services."""

    def now(self) -> datetime:
        """Return the current UTC instant."""


class SystemClock:
    """Production clock; the only domain-facing implementation using wall time."""

    def now(self) -> datetime:
        """Return the current timezone-aware UTC instant."""
        return datetime.now(UTC)
