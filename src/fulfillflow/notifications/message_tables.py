"""Notifications owns its inbox, quarantine and rearm audit, without an outbox."""

from fulfillflow.messaging.tables import notification_message_tables
from fulfillflow.notifications.base import NotificationsBase

tables = notification_message_tables(NotificationsBase.metadata)
