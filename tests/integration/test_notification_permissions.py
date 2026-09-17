"""Exercise the actual v1.3 broker users, bindings and permissions."""

import os
from urllib.parse import urlsplit

import aio_pika
import pytest
from aiormq.exceptions import ChannelAccessRefused
from tests.notification_support import notification_message

from fulfillflow.messaging.amqp import declare_flow, publish

FLOW = "shipment.status_changed.v1"
PASSWORDS = {
    "core": "fulfillflow-v12-core-amqp-local-2026",
    "tracking": "fulfillflow-v12-tracking-amqp-local-2026",
    "notifications": "fulfillflow-v13-notifications-local-2026",
}


def owner_url(owner: str) -> str:
    url = os.environ.get("TEST_AMQP_URL")
    assert url, "TEST_AMQP_URL is required; broker ACL verification cannot be skipped"
    parsed = urlsplit(url)
    return f"amqp://{owner}:{PASSWORDS[owner]}@{parsed.hostname}:{parsed.port}/fulfillflow-v13"


async def test_core_publishes_before_notifications_consumes() -> None:
    core = await aio_pika.connect(owner_url("core"), timeout=10)
    async with core:
        channel = await core.channel(publisher_confirms=True, on_return_raises=True)
        await channel.declare_exchange(FLOW, aio_pika.ExchangeType.DIRECT, durable=True)
        await publish(channel, notification_message())
    notifications = await aio_pika.connect(owner_url("notifications"), timeout=10)
    async with notifications:
        channel = await notifications.channel()
        await declare_flow(channel, FLOW)
        queue = await channel.get_queue(f"{FLOW}.queue")
        message = await queue.get(timeout=5)
        assert message is not None
        assert message.message_id == str(notification_message().message_id)
        await message.ack()


@pytest.mark.parametrize(
    ("owner", "operation"),
    [("core", "consume"), ("tracking", "publish"), ("notifications", "publish")],
)
async def test_owner_cannot_cross_notification_permissions(owner: str, operation: str) -> None:
    connection = await aio_pika.connect(owner_url(owner), timeout=10)
    async with connection:
        channel = await connection.channel(publisher_confirms=True, on_return_raises=True)
        with pytest.raises(ChannelAccessRefused, match="ACCESS_REFUSED"):
            if operation == "consume":
                queue = await channel.get_queue(f"{FLOW}.queue", ensure=False)
                await queue.get(timeout=5)
            else:
                exchange = await channel.get_exchange(FLOW, ensure=False)
                await exchange.publish(aio_pika.Message(body=b"denied"), routing_key=FLOW)
