# Copyright (c) Microsoft Corporation.
# Licensed under the MIT License.

"""Azure Cosmos DB access for account profiles and retention actions.

Uses the same identity-based connection as the change feed trigger (``COSMOS__accountEndpoint``)
with the app's managed identity in Azure, or your ``az login`` identity locally. The account
has local (key) auth disabled. Files whose names start with ``_`` are helpers, not tools.

The app's identity can only *read* the monitored ``accounts`` container (see
infra/app/cosmos.bicep), so nothing here can write back to it and start a trigger loop.
"""

from __future__ import annotations

import os
from functools import cache
from typing import Any

from azure.cosmos import ContainerProxy, CosmosClient
from azure.cosmos.exceptions import CosmosResourceExistsError, CosmosResourceNotFoundError

_SYSTEM_FIELDS = ("_rid", "_self", "_etag", "_attachments", "_lsn")


@cache
def _database():
    from azure.identity import DefaultAzureCredential

    endpoint = os.environ.get("COSMOS__accountEndpoint", "")
    if not endpoint:
        raise RuntimeError("COSMOS__accountEndpoint is not set.")
    credential = DefaultAzureCredential(managed_identity_client_id=os.environ.get("AZURE_CLIENT_ID"))
    client = CosmosClient(endpoint, credential=credential)
    return client.get_database_client(os.environ.get("COSMOS_DATABASE", "retention"))


def _accounts() -> ContainerProxy:
    return _database().get_container_client(os.environ.get("ACCOUNTS_CONTAINER", "accounts"))


def _actions() -> ContainerProxy:
    return _database().get_container_client(os.environ.get("RETENTION_ACTIONS_CONTAINER", "retention-actions"))


def get_account(account_id: str) -> dict[str, Any] | None:
    """Read the current account document, or ``None`` if it doesn't exist."""
    try:
        doc = _accounts().read_item(item=account_id, partition_key=account_id)
    except CosmosResourceNotFoundError:
        return None
    return {k: v for k, v in doc.items() if k not in _SYSTEM_FIELDS}


def create_action_once(document: dict[str, Any]) -> bool:
    """Create the retention action. Return ``False`` if one with the same id already exists."""
    try:
        _actions().create_item(document)
    except CosmosResourceExistsError:
        return False
    return True
