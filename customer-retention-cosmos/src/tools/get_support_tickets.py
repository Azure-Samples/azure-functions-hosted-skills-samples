# Copyright (c) Microsoft Corporation.
# Licensed under the MIT License.

"""Recent support tickets backed by sample data."""

from typing import Any

from _sample_data import load, normalize_account_id


def get_support_tickets(account_id: str) -> dict[str, Any]:
    """Get the account's recent support tickets with status, severity (sev1 is most severe), and a short summary.

    Args:
        account_id: Account ID, for example ``ACC-1001``.
    """
    account_id = normalize_account_id(account_id)
    tickets = load("support_tickets").get(account_id)
    if tickets is None:
        return {"error": f"No support history for {account_id}.", "account_id": account_id}
    return {"account_id": account_id, "tickets": tickets}
