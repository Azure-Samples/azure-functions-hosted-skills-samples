# Copyright (c) Microsoft Corporation.
# Licensed under the MIT License.

"""Blob Storage access for published briefings.

Uses the ``AzureWebJobsStorage`` connection: a connection string locally (Azurite),
or the ``AzureWebJobsStorage__blobServiceUri`` setting with the app's managed identity
in Azure.
"""

from __future__ import annotations

import os
from functools import cache

from azure.core.exceptions import ResourceExistsError
from azure.storage.blob import BlobServiceClient, ContainerClient, ContentSettings


@cache
def _service() -> BlobServiceClient:
    connection_string = os.environ.get("AzureWebJobsStorage", "")
    if connection_string:
        return BlobServiceClient.from_connection_string(connection_string)

    from azure.identity import DefaultAzureCredential

    credential = DefaultAzureCredential(managed_identity_client_id=os.environ.get("AZURE_CLIENT_ID"))
    return BlobServiceClient(os.environ["AzureWebJobsStorage__blobServiceUri"], credential=credential)


def container_name() -> str:
    return os.environ.get("BRIEFINGS_CONTAINER", "briefings")


@cache
def _briefings() -> ContainerClient:
    container = _service().get_container_client(container_name())
    if os.environ.get("AzureWebJobsStorage"):
        # Local development (Azurite): create the container on first use. In Azure it is
        # provisioned by infra/main.bicep.
        try:
            container.create_container()
        except ResourceExistsError:
            pass
    return container


def save_once(name: str, content: str, content_type: str) -> bool:
    """Write ``name`` unless it already exists. Return ``False`` if it exists."""
    blob = _briefings().get_blob_client(name)
    try:
        blob.upload_blob(content, overwrite=False, content_settings=ContentSettings(content_type=content_type))
    except ResourceExistsError:
        return False
    return True
