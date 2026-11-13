"""Churn risk signals computed in code from the live account and its support tickets."""

from datetime import UTC, datetime
from typing import Any

import _cosmos
from _policy import churn_signals
from _sample_data import load, normalize_account_id


def assess_churn_signals(account_id: str) -> dict[str, Any]:
    """Compute churn risk for an account: usage change against baseline, seat utilization, days to renewal,
    open high-severity tickets, a risk level (low, medium, high), and whether a retention action is recommended.

    Args:
        account_id: Account ID, for example ``ACC-1001``.
    """
    account_id = normalize_account_id(account_id)
    account = _cosmos.get_account(account_id)
    if account is None:
        return {"error": f"Account {account_id} was not found.", "account_id": account_id}
    tickets = load("support_tickets").get(account_id, [])
    result = churn_signals(account, tickets, datetime.now(UTC).date())
    result["usage_snapshot_id"] = (account.get("usage") or {}).get("snapshot_id")
    return result
