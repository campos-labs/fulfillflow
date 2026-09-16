"""Core-owned transport CLI."""

from fulfillflow.core.message_tables import tables
from fulfillflow.messaging.cli import main

if __name__ == "__main__":
    main("core", tables)
