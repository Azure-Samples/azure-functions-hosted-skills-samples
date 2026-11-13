# Copyright (c) Microsoft Corporation.
# Licensed under the MIT License.

"""Plan and pricing catalog with the retention offer policy, backed by sample data."""

from typing import Any

from _policy import offer_limits
from _sample_data import load


def get_plan_catalog(tier: str) -> dict[str, Any]:
    """Get all plans with per-seat prices, and the retention offers allowed for an account tier:
    maximum discount, when an offer needs manager approval, the success package, and seat rightsizing.

    Args:
        tier: The account's tier: ``starter``, ``business``, or ``enterprise``.
    """
    catalog = load("plans")
    tier = tier.strip().lower()
    if tier not in catalog["plans"]:
        return {"error": f"Unknown tier {tier!r}.", "tiers": sorted(catalog["plans"])}
    return {"currency": catalog["currency"], "plans": catalog["plans"], "retention_offers": offer_limits(tier)}
