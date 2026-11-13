"""Churn signals and retention offer policy, computed in code.

The hosted skill interprets these results; it never does the math or sets the limits.
"""

from __future__ import annotations

from datetime import date
from decimal import ROUND_HALF_UP, Decimal
from typing import Any

from _sample_data import load

USAGE_DROP_ACTION_PERCENT = -15
HIGH_SEVERITIES = {"sev1", "sev2"}


def _round(value: Decimal | float, places: str = "0.1") -> float:
    return float(Decimal(str(value)).quantize(Decimal(places), rounding=ROUND_HALF_UP))


def days_until(iso_date: str | None, today: date) -> int | None:
    if not iso_date:
        return None
    try:
        return (date.fromisoformat(iso_date[:10]) - today).days
    except ValueError:
        return None


def churn_signals(account: dict[str, Any], tickets: list[dict[str, Any]], today: date) -> dict[str, Any]:
    """Score churn risk from usage, renewal timing, open tickets, and seat utilization."""
    usage = account.get("usage") or {}
    active = usage.get("active_users_30d")
    baseline = usage.get("baseline_active_users")
    seats = account.get("seats") or 0

    usage_change = None
    if isinstance(active, (int, float)) and isinstance(baseline, (int, float)) and baseline > 0:
        usage_change = _round((Decimal(str(active)) - Decimal(str(baseline))) / Decimal(str(baseline)) * 100)
    seat_utilization = _round(Decimal(str(active)) / Decimal(seats) * 100) if seats and active is not None else None
    renewal_days = days_until(account.get("renewal_date"), today)
    open_high = [t["ticket_id"] for t in tickets if t.get("status") == "open" and t.get("severity") in HIGH_SEVERITIES]

    score = 0
    signals: list[str] = []
    if usage_change is not None and usage_change <= -50:
        score += 3
        signals.append(f"Active users down {abs(usage_change)}% against baseline (severe).")
    elif usage_change is not None and usage_change <= -30:
        score += 2
        signals.append(f"Active users down {abs(usage_change)}% against baseline (significant).")
    elif usage_change is not None and usage_change <= USAGE_DROP_ACTION_PERCENT:
        score += 1
        signals.append(f"Active users down {abs(usage_change)}% against baseline (moderate).")
    if renewal_days is not None and renewal_days <= 30:
        score += 2
        signals.append(f"Renewal in {renewal_days} days.")
    elif renewal_days is not None and renewal_days <= 90:
        score += 1
        signals.append(f"Renewal in {renewal_days} days.")
    if open_high:
        score += 1
        signals.append(f"Open high-severity support tickets: {', '.join(open_high)}.")
    if seat_utilization is not None and seat_utilization < 50:
        score += 1
        signals.append(f"Only {seat_utilization}% of paid seats are active.")

    risk_level = "high" if score >= 4 else "medium" if score >= 2 else "low"
    # This skill reacts to usage changes: renewal timing and tickets raise the risk level,
    # but on their own they don't start a retention action.
    usage_dropped = usage_change is not None and usage_change <= USAGE_DROP_ACTION_PERCENT
    return {
        "account_id": account.get("id"),
        "tier": account.get("tier"),
        "usage_change_percent": usage_change,
        "seat_utilization_percent": seat_utilization,
        "days_to_renewal": renewal_days,
        "open_high_severity_tickets": open_high,
        "risk_score": score,
        "risk_level": risk_level,
        "signals": signals,
        "action_recommended": usage_dropped and risk_level != "low",
    }


def offer_limits(tier: str) -> dict[str, Any]:
    policy = load("plans")["retention_policy"]
    return {
        "tier": tier,
        "max_discount_percent": policy["max_discount_percent"].get(tier, 0),
        "approval_required_above_discount_percent": policy["approval_required_above_discount_percent"],
        "approval_required_above_offer_value": policy["approval_required_above_offer_value"],
        "success_package": policy["success_package"],
        "seat_rightsizing": policy["seat_rightsizing"],
        "rules": policy["rules"],
    }


def evaluate_offer(tier: str, annual_contract_value: float, offer_type: str, discount_percent: float) -> dict[str, Any]:
    """Check an offer against the tier's limits and decide whether it needs approval.

    Returns ``{"allowed": False, "error": ...}`` for offers outside policy.
    """
    limits = offer_limits(tier)
    max_discount = limits["max_discount_percent"]
    if discount_percent < 0:
        return {"allowed": False, "error": "discount_percent cannot be negative."}
    if offer_type != "discount" and discount_percent:
        return {"allowed": False, "error": "Only a 'discount' offer can have a discount_percent."}
    if offer_type == "discount" and not 0 < discount_percent <= max_discount:
        return {
            "allowed": False,
            "error": f"A {tier} account can get a discount of at most {max_discount}%; {discount_percent}% was requested.",
            "max_discount_percent": max_discount,
        }

    if offer_type == "discount":
        value = _round(Decimal(str(annual_contract_value)) * Decimal(str(discount_percent)) / 100, "0.01")
    elif offer_type == "success_package":
        value = float(limits["success_package"]["value"])
    else:
        value = 0.0

    reasons = []
    if discount_percent > limits["approval_required_above_discount_percent"]:
        reasons.append(f"Discount above {limits['approval_required_above_discount_percent']}%.")
    if value > limits["approval_required_above_offer_value"]:
        reasons.append(f"Offer worth more than {limits['approval_required_above_offer_value']} per year.")
    return {
        "allowed": True,
        "offer_value": value,
        "max_discount_percent": max_discount,
        "needs_approval": bool(reasons),
        "approval_reasons": reasons,
    }
