"""Schemas composed from more than one public business module."""

from fulfillflow.orders.schemas import OrderRead
from fulfillflow.shipments.schemas import ShipmentSummaryRead


class OrderDetailRead(OrderRead):
    """Order detail plus Shipment-owned summaries."""

    shipments: list[ShipmentSummaryRead]
