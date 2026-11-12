"""Stores return recommendations in Azure Blob Storage.

Uses the ``AzureWebJobsStorage`` connection: a connection string locally (Azurite),
or the ``AzureWebJobsStorage__blobServiceUri`` setting with the app's managed identity
in Azure.
"""

from __future__ import annotations

import json
import os
from typing import Any

from azure.core.exceptions import ResourceExistsError
from azure.storage.blob import ContainerClient

_container: ContainerClient | None = None


def get_container() -> ContainerClient:
    global _container
    if _container is not None:
        return _container

    container_name = os.environ.get("RECOMMENDATIONS_CONTAINER", "return-recommendations")
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

        credential = DefaultAzureCredential(
            managed_identity_client_id=os.environ.get("AZURE_CLIENT_ID")
        )
        container = ContainerClient(
            os.environ["AzureWebJobsStorage__blobServiceUri"], container_name, credential=credential
        )

    _container = container
    return container


def save_once(request_id: str, document: dict[str, Any]) -> bool:
    """Write the document for ``request_id``. Return ``False`` if one already exists."""
    blob = get_container().get_blob_client(f"{request_id}.json")
    try:
        blob.upload_blob(
            json.dumps(document, indent=2),
            overwrite=False,
            content_type="application/json",
        )
    except ResourceExistsError:
        return False
    return True
