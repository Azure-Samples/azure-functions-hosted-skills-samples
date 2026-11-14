# Copyright (c) Microsoft Corporation.
# Licensed under the MIT License.

"""Blob Storage records for helpdesk replies, tickets, and escalations.

Everything lives in one container (``HELPDESK_CONTAINER``) under ``replies/``,
``deliveries/``, ``tickets/``, and ``escalations/``. Each record is keyed by the Teams
message ID and written with ``overwrite=False``, so a redelivered message never creates
a second ticket, escalation, or reply. ``deliveries/`` is written only after the reply
was posted in Teams, so a reply that was recorded but never delivered stays retryable.

Uses the ``AzureWebJobsStorage`` connection: a connection string locally (Azurite),
or the ``AzureWebJobsStorage__blobServiceUri`` setting with the app's managed identity
in Azure. Files whose names start with ``_`` are helpers, not tools.
"""

from __future__ import annotations

import json
import os
import re
from functools import cache
from typing import Any

from azure.core.exceptions import ResourceExistsError, ResourceNotFoundError
from azure.storage.blob import BlobServiceClient, ContainerClient

_MESSAGE_ID = re.compile(r"^[A-Za-z0-9._-]{1,128}$")
PREFIXES = ("replies", "deliveries", "tickets", "escalations")


@cache
def _service() -> BlobServiceClient:
    connection_string = os.environ.get("AzureWebJobsStorage", "")
    if connection_string:
        return BlobServiceClient.from_connection_string(connection_string)

    from azure.identity import DefaultAzureCredential

    credential = DefaultAzureCredential(managed_identity_client_id=os.environ.get("AZURE_CLIENT_ID"))
    return BlobServiceClient(os.environ["AzureWebJobsStorage__blobServiceUri"], credential=credential)


@cache
def _container() -> ContainerClient:
    container = _service().get_container_client(os.environ.get("HELPDESK_CONTAINER", "helpdesk"))
    if os.environ.get("AzureWebJobsStorage"):
        # Local development (Azurite): create the container on first use. In Azure it is
        # provisioned by infra/main.bicep.
        try:
            container.create_container()
        except ResourceExistsError:
            pass
    return container


def check_message_id(message_id: str) -> str:
    """Return the message ID if it is safe to use in a blob name, else raise ``ValueError``."""
    value = (message_id or "").strip()
    if not _MESSAGE_ID.match(value):
        raise ValueError("message_id must be 1-128 letters, digits, '.', '-' or '_' (the Teams message 'id').")
    return value


def save_once(prefix: str, message_id: str, document: dict[str, Any]) -> bool:
    """Write ``<prefix>/<message_id>.json``. Return ``False`` if it already exists."""
    if prefix not in PREFIXES:
        raise ValueError(f"Unknown record type {prefix!r}")
    blob = _container().get_blob_client(f"{prefix}/{check_message_id(message_id)}.json")
    try:
        blob.upload_blob(json.dumps(document, indent=2), overwrite=False, content_type="application/json")
    except ResourceExistsError:
        return False
    return True


def load(prefix: str, message_id: str) -> dict[str, Any] | None:
    if prefix not in PREFIXES:
        raise ValueError(f"Unknown record type {prefix!r}")
    try:
        data = _container().download_blob(f"{prefix}/{check_message_id(message_id)}.json").readall()
    except ResourceNotFoundError:
        return None
    return json.loads(data)
