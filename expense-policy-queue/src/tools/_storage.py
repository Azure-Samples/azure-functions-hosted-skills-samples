# Copyright (c) Microsoft Corporation.
# Licensed under the MIT License.

"""Blob and queue access for the expense tools.

Uses the ``AzureWebJobsStorage`` connection: a connection string locally (Azurite),
or the ``AzureWebJobsStorage__blobServiceUri`` / ``__queueServiceUri`` settings with the
app's managed identity in Azure. Files whose names start with ``_`` are helpers, not tools.
"""

from __future__ import annotations

import json
import os
import re
from functools import cache
from typing import Any

from azure.core.exceptions import ResourceExistsError, ResourceNotFoundError
from azure.storage.blob import BlobServiceClient, ContainerClient
from azure.storage.queue import QueueServiceClient

OUTPUT_QUEUES = {
    "approve": "expense-approved",
    "review": "expense-review",
    "flag": "expense-flagged",
}
MAX_POLICY_BYTES = 64 * 1024
POLICY_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,99}\.md$")


def _connection_string() -> str:
    return os.environ.get("AzureWebJobsStorage", "")


@cache
def _credential():
    from azure.identity import DefaultAzureCredential

    return DefaultAzureCredential(managed_identity_client_id=os.environ.get("AZURE_CLIENT_ID"))


@cache
def _blob_service() -> BlobServiceClient:
    if _connection_string():
        return BlobServiceClient.from_connection_string(_connection_string())
    return BlobServiceClient(os.environ["AzureWebJobsStorage__blobServiceUri"], credential=_credential())


@cache
def _queue_service() -> QueueServiceClient:
    if _connection_string():
        return QueueServiceClient.from_connection_string(_connection_string())
    return QueueServiceClient(os.environ["AzureWebJobsStorage__queueServiceUri"], credential=_credential())


def _container(setting: str, default: str) -> ContainerClient:
    container = _blob_service().get_container_client(os.environ.get(setting, default))
    if _connection_string():
        # Local development (Azurite): create the container on first use. In Azure it is
        # provisioned by infra/main.bicep.
        try:
            container.create_container()
        except ResourceExistsError:
            pass
    return container


@cache
def policies() -> ContainerClient:
    return _container("POLICIES_CONTAINER", "policies")


@cache
def decisions() -> ContainerClient:
    return _container("DECISIONS_CONTAINER", "decisions")


def policy_name(name: str) -> str:
    """Normalize ``policies/x.md`` or ``/policies/x.md`` to ``x.md`` and reject anything else."""
    value = name.strip().replace("\\", "/").lstrip("/")
    prefix = f"{os.environ.get('POLICIES_CONTAINER', 'policies')}/"
    if value.startswith(prefix):
        value = value[len(prefix):]
    if not POLICY_NAME.match(value) or ".." in value:
        raise ValueError(f"Invalid policy document name {name!r}. Use a name returned by list_policies, such as travel-policy.md.")
    return value


def applies_to(text: str) -> str:
    """Return the text after the document's ``**Applies to:**`` marker, up to the next blank line."""
    match = re.search(r"\*\*Applies to:\*\*\s*(.+?)(?:\n\s*\n|$)", text, re.DOTALL)
    return " ".join(match.group(1).split()) if match else ""


def list_policy_documents() -> list[dict[str, Any]]:
    container = policies()
    documents = []
    for blob in container.list_blobs():
        if not blob.name.endswith(".md") or "/" in blob.name or blob.size > MAX_POLICY_BYTES:
            continue
        text = container.download_blob(blob.name).readall().decode("utf-8", errors="replace")
        documents.append({
            "name": blob.name,
            "applies_to": applies_to(text),
            "last_modified": blob.last_modified.isoformat() if blob.last_modified else "",
        })
    return documents


def read_policy_document(name: str) -> dict[str, Any]:
    blob = policies().get_blob_client(policy_name(name))
    try:
        properties = blob.get_blob_properties()
    except ResourceNotFoundError:
        raise LookupError(f"Policy document {blob.blob_name!r} does not exist in the policies container.") from None
    if properties.size > MAX_POLICY_BYTES:
        raise ValueError(f"Policy document is {properties.size} bytes; the limit is {MAX_POLICY_BYTES}.")
    return {
        "name": blob.blob_name,
        "last_modified": properties.last_modified.isoformat() if properties.last_modified else "",
        "content": blob.download_blob().readall().decode("utf-8", errors="replace"),
    }


def claim_decision(expense_id: str, document: dict[str, Any]) -> bool:
    """Write the decision marker for ``expense_id``. Return ``False`` if it already exists."""
    blob = decisions().get_blob_client(f"{expense_id}.json")
    try:
        blob.upload_blob(json.dumps(document, indent=2), overwrite=False, content_type="application/json")
    except ResourceExistsError:
        return False
    return True


def release_decision(expense_id: str) -> None:
    try:
        decisions().delete_blob(f"{expense_id}.json")
    except ResourceNotFoundError:
        pass


def send_to_queue(queue_name: str, message: str) -> str:
    if queue_name not in OUTPUT_QUEUES.values():
        raise ValueError(f"{queue_name!r} is not an output queue.")
    queue = _queue_service().get_queue_client(queue_name)
    if _connection_string():
        try:
            queue.create_queue()
        except ResourceExistsError:
            pass
    return queue.send_message(message).id
