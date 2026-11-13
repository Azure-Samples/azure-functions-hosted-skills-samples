# Copyright (c) Microsoft Corporation.
# Licensed under the MIT License.

"""Order lookup tool backed by sample data."""

from __future__ import annotations

from datetime import date, datetime
from typing import Any

from _sample_data import load


def _parse_date(value: str) -> date:
    return datetime.fromisoformat(value.replace("Z", "+00:00")).date()


def get_order(order_id: str, as_of: str = "") -> dict[str, Any]:
    """Get an order with its items, product categories, delivery date, and delivery notes.

    Args:
        order_id: Order ID from the return request, for example ``ORD-5001``.
        as_of: Optional ISO-8601 date or timestamp, usually the request's ``received_at``.
            When provided, the result includes ``days_since_delivery`` calculated in code.
    """
    order = load("orders").get(order_id.strip().upper())
    if order is None:
        return {"error": f"Order {order_id} was not found.", "order_id": order_id}

    result = dict(order)
    if as_of:
        try:
            delta = _parse_date(as_of) - _parse_date(order["delivered_at"])
        except ValueError:
            result["days_since_delivery_error"] = f"Could not parse as_of value {as_of!r}."
        else:
            result["days_since_delivery"] = delta.days
    return result
