"""Unit coverage for deterministic simulated Notification records."""

from datetime import UTC, datetime
from uuid import UUID

import pytest

from fulfillflow.notifications.public import (
    ExpectedNotificationFailure,
    NotificationChannel,
    NotificationStatus,
    RenderedNotification,
    build_notification,
    render_notification,
)

NOW = datetime(2026, 8, 30, 12, 0, tzinfo=UTC)
NOTIFICATION_ID = UUID("00000000-0000-4000-8000-000000000501")
SHIPMENT_ID = UUID("00000000-0000-4000-8000-000000000502")
TRACKING_EVENT_ID = UUID("00000000-0000-4000-8000-000000000503")


@pytest.mark.parametrize(
    ("resulting_status", "template_key", "message"),
    [
        ("PENDING", "shipment_pending", "Your shipment is pending."),
        ("POSTED", "shipment_posted", "Your shipment has been posted."),
        ("IN_TRANSIT", "shipment_in_transit", "Your shipment is in transit."),
        (
            "OUT_FOR_DELIVERY",
            "shipment_out_for_delivery",
            "Your shipment is out for delivery.",
        ),
        ("DELIVERED", "shipment_delivered", "Your shipment was delivered."),
        (
            "EXCEPTION",
            "shipment_exception",
            "Your shipment has a delivery exception.",
        ),
        ("RETURNED", "shipment_returned", "Your shipment was returned."),
        ("CANCELLED", "shipment_cancelled", "Your shipment was cancelled."),
    ],
)
def test_every_canonical_status_has_deterministic_content(
    resulting_status: str,
    template_key: str,
    message: str,
) -> None:
    rendered = render_notification(resulting_status)

    assert rendered == RenderedNotification(template_key, message)


def test_successful_render_builds_email_simulation_record() -> None:
    notification = build_notification(
        notification_id=NOTIFICATION_ID,
        shipment_id=SHIPMENT_ID,
        tracking_event_id=TRACKING_EVENT_ID,
        recipient="  recipient@example.test  ",
        resulting_status="DELIVERED",
        created_at=NOW,
    )

    assert notification.id == NOTIFICATION_ID
    assert notification.shipment_id == SHIPMENT_ID
    assert notification.tracking_event_id == TRACKING_EVENT_ID
    assert notification.channel is NotificationChannel.EMAIL
    assert notification.recipient == "recipient@example.test"
    assert notification.template_key == "shipment_delivered"
    assert notification.message == "Your shipment was delivered."
    assert notification.status is NotificationStatus.SIMULATED
    assert notification.error_detail is None
    assert notification.created_at == NOW
    assert notification.simulated_at == NOW


def test_expected_failure_builds_sanitized_failed_record() -> None:
    secret = "provider-secret-must-not-be-persisted"

    def fail_rendering(resulting_status: str) -> RenderedNotification:
        del resulting_status
        raise ExpectedNotificationFailure(secret)

    notification = build_notification(
        notification_id=NOTIFICATION_ID,
        shipment_id=SHIPMENT_ID,
        tracking_event_id=TRACKING_EVENT_ID,
        recipient="recipient@example.test",
        resulting_status="DELIVERED",
        created_at=NOW,
        renderer=fail_rendering,
    )

    assert notification.status is NotificationStatus.FAILED
    assert notification.template_key == "shipment_notification_failed"
    assert notification.message == "The shipment notification could not be simulated."
    assert notification.error_detail == "Notification rendering or simulation failed."
    assert secret not in notification.message
    assert secret not in notification.error_detail
    assert notification.created_at == NOW
    assert notification.simulated_at is None


def test_unknown_status_is_an_expected_failure_without_echoing_external_text() -> None:
    untrusted_status = "DELIVERED: <script>arbitrary carrier content</script>"

    with pytest.raises(ExpectedNotificationFailure) as error:
        render_notification(untrusted_status)

    assert untrusted_status not in str(error.value)


def test_unexpected_renderer_failure_is_not_downgraded_to_failed_record() -> None:
    def broken_renderer(resulting_status: str) -> RenderedNotification:
        del resulting_status
        raise RuntimeError("unexpected infrastructure failure")

    with pytest.raises(RuntimeError, match="unexpected infrastructure failure"):
        build_notification(
            notification_id=NOTIFICATION_ID,
            shipment_id=SHIPMENT_ID,
            tracking_event_id=TRACKING_EVENT_ID,
            recipient="recipient@example.test",
            resulting_status="DELIVERED",
            created_at=NOW,
            renderer=broken_renderer,
        )
