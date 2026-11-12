"""Purchase order and goods receipt lookup backed by sample data."""

from __future__ import annotations

from typing import Any

from _sample_data import load


def get_purchase_order(po_number: str) -> dict[str, Any]:
    """Get a purchase order with its vendor, line items, agreed unit prices, and received quantities.

    Args:
        po_number: PO number printed on the invoice, for example ``PO-4001``.
    """
    order = load("purchase_orders").get(po_number.strip().upper())
    if order is None:
        return {"error": f"Purchase order {po_number} was not found.", "po_number": po_number}
    return order
