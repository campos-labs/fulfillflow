"""Tracking owns these tables in its own database."""

from fulfillflow.messaging.tables import message_tables
from fulfillflow.tracking.base import TrackingBase

tables = message_tables(TrackingBase.metadata, "tracking")
