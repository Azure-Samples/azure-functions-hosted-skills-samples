# Copyright (c) Microsoft Corporation.
# Licensed under the MIT License.

"""Stores investigation reports and reads offloaded alert payloads in Azure Blob Storage.

Uses the ``AzureWebJobsStorage`` connection: a connection string locally (Azurite),
or the ``AzureWebJobsStorage__blobServiceUri`` setting with the app's managed identity
in Azure.
"""

from __future__ import annotations

import json
import os
from typing import Any

from azure.core.exceptions import ResourceExistsError, ResourceNotFoundError
from azure.storage.blob import ContainerClient

_containers: dict[str, ContainerClient] = {}


def get_container(container_name: str | None = None) -> ContainerClient:
    container_name = container_name or os.environ.get("INVESTIGATIONS_CONTAINER", "investigations")
    if container_name in _containers:
        return _containers[container_name]

    connection_string = os.environ.get("AzureWebJobsStorage", "")
    if connection_string:
        # Local development (Azurite): create the container on first use.
        container = ContainerClient.from_connection_string(connection_string, container_name)
        try:
            container.create_container()
        except ResourceExistsError:
            pass
    else:
        # Azure: the container is provisioned by infra/main.bicep.
        from azure.identity import DefaultAzureCredential

        credential = DefaultAzureCredential(managed_identity_client_id=os.environ.get("AZURE_CLIENT_ID"))
        container = ContainerClient(
            os.environ["AzureWebJobsStorage__blobServiceUri"], container_name, credential=credential
        )

    _containers[container_name] = container
    return container


def save_once(group_id: str, document: dict[str, Any]) -> bool:
    """Write the report for ``group_id``. Return ``False`` if one already exists."""
    blob = get_container().get_blob_client(f"{group_id}.json")
    try:
        blob.upload_blob(json.dumps(document, indent=2), overwrite=False, content_type="application/json")
    except ResourceExistsError:
        return False
    return True


def load_payload(blob_name: str) -> str | None:
    """Read an alert payload the router offloaded because it was too large for a queue message."""
    container = get_container(os.environ.get("PAYLOADS_CONTAINER", "alert-payloads"))
    try:
        return container.download_blob(blob_name).readall().decode("utf-8")
    except ResourceNotFoundError:
        return None
