"""Return policy lookup tool backed by sample data."""

from __future__ import annotations

from typing import Any

from _sample_data import load


def get_return_policy(category: str) -> dict[str, Any]:
    """Get the return policy for a product category, including the return window and exceptions.

    Args:
        category: Product category from the order item, for example ``outdoor_gear`` or ``apparel``.
    """
    policies = load("return_policies")
    policy = policies.get(category.strip().lower())
    if policy is None:
        return {
            "error": f"No return policy found for category {category!r}.",
            "known_categories": sorted(policies),
        }
    return policy
