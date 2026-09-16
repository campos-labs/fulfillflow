"""Core owns these tables in its own database."""

from fulfillflow.db.base import Base
from fulfillflow.messaging.tables import message_tables

tables = message_tables(Base.metadata)
