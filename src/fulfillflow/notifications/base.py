"""Notifications service's independent SQLAlchemy metadata registry."""

from sqlalchemy import MetaData
from sqlalchemy.orm import DeclarativeBase

from fulfillflow.db.base import NAMING_CONVENTION


class NotificationsBase(DeclarativeBase):
    metadata = MetaData(naming_convention=NAMING_CONVENTION)
