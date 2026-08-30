"""Stable technical primitives shared by business modules."""

from fulfillflow.shared.clock import Clock, SystemClock
from fulfillflow.shared.ids import new_uuid
from fulfillflow.shared.pagination import Page

__all__ = ["Clock", "Page", "SystemClock", "new_uuid"]
