"""Technical pagination result shared by query services."""

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class Page[ItemT]:
    """One stable page plus the total matching row count."""

    items: list[ItemT]
    page: int
    page_size: int
    total: int
