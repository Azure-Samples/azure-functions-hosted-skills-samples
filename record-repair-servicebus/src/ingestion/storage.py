"""Blob Storage output for the pipeline.

Uses the ``AzureWebJobsStorage`` connection: a connection string locally (Azurite), or the
``AzureWebJobsStorage__blobServiceUri`` setting with the app's managed identity in Azure.
"""

from __future__ import annotations

import json
import os
from typing import Any

from azure.core.exceptions import ResourceExistsError
from azure.storage.blob import ContainerClient, ContentSettings

_containers: dict[str, ContainerClient] = {}


def processed_container_name() -> str:
    return os.environ.get("PROCESSED_CONTAINER", "processed-data")


def failures_container_name() -> str:
    return os.environ.get("FAILURES_CONTAINER", "repair-failures")


def get_container(name: str) -> ContainerClient:
    if name in _containers:
        return _containers[name]

    connection_string = os.environ.get("AzureWebJobsStorage", "")
    if connection_string:
        # Local development (Azurite): create the container on first use.
        container = ContainerClient.from_connection_string(connection_string, name)
        try:
            container.create_container()
        except ResourceExistsError:
            pass
    else:
        # Azure: the containers are provisioned by infra/main.bicep.
        from azure.identity import DefaultAzureCredential

        credential = DefaultAzureCredential(managed_identity_client_id=os.environ.get("AZURE_CLIENT_ID"))
        container = ContainerClient(os.environ["AzureWebJobsStorage__blobServiceUri"], name, credential=credential)

    _containers[name] = container
    return container


def write_once(container_name: str, blob_name: str, document: dict[str, Any]) -> bool:
    """Write ``document`` as JSON. Return ``False`` if the blob already exists."""
    blob = get_container(container_name).get_blob_client(blob_name)
    try:
        blob.upload_blob(
            json.dumps(document, indent=2, allow_nan=False),
            overwrite=False,
            content_settings=ContentSettings(content_type="application/json"),
        )
    except ResourceExistsError:
        return False
    return True
