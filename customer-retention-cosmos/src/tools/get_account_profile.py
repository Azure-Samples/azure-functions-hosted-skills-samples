"""Account profile from Cosmos DB plus usage history from sample data."""

from typing import Any

import _cosmos
from _sample_data import load, normalize_account_id


def get_account_profile(account_id: str) -> dict[str, Any]:
    """Get the current account document (tier, seats, contract value, renewal date, contacts, current usage)
    and its monthly active-user history for the last six months (oldest first), features adopted, and NPS.

    Args:
        account_id: Account ID, for example ``ACC-1001``.
    """
    account_id = normalize_account_id(account_id)
    account = _cosmos.get_account(account_id)
    if account is None:
        return {"error": f"Account {account_id} was not found.", "account_id": account_id}
    history = load("usage_history").get(account_id, {})
    return {**account, "usage_history": history}
