"""Support history lookup tool backed by sample data."""

from __future__ import annotations

from typing import Any

from _sample_data import load


def get_support_history(customer_id: str) -> dict[str, Any]:
    """Get a customer's support tickets, return count, and account flags.

    Args:
        customer_id: Customer ID from the return request, for example ``C-1001``.
    """
    customer = load("customers").get(customer_id.strip().upper())
    if customer is None:
        return {"error": f"Customer {customer_id} was not found.", "customer_id": customer_id}
    return customer
