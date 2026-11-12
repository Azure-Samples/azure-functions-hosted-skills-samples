"""Blob Storage access for invoices and invoice reviews.

Uses the ``AzureWebJobsStorage`` connection: a connection string locally (Azurite),
or the ``AzureWebJobsStorage__blobServiceUri`` setting with the app's managed identity
in Azure. Files whose names start with ``_`` are helpers, not tools.
"""

from __future__ import annotations

import json
import os
from functools import cache
from pathlib import PurePosixPath
from typing import Any

from azure.core.exceptions import ResourceExistsError
from azure.storage.blob import BlobServiceClient, ContainerClient

MAX_INVOICE_BYTES = 10 * 1024 * 1024


@cache
def _service() -> BlobServiceClient:
    connection_string = os.environ.get("AzureWebJobsStorage", "")
    if connection_string:
        return BlobServiceClient.from_connection_string(connection_string)

    from azure.identity import DefaultAzureCredential

    credential = DefaultAzureCredential(managed_identity_client_id=os.environ.get("AZURE_CLIENT_ID"))
    return BlobServiceClient(os.environ["AzureWebJobsStorage__blobServiceUri"], credential=credential)


def invoices_container_name() -> str:
    return os.environ.get("INVOICES_CONTAINER", "invoices")


@cache
def _reviews() -> ContainerClient:
    container = _service().get_container_client(os.environ.get("REVIEWS_CONTAINER", "invoice-reviews"))
    if os.environ.get("AzureWebJobsStorage"):
        # Local development (Azurite): create the container on first use. In Azure it is
        # provisioned by infra/main.bicep.
        try:
            container.create_container()
        except ResourceExistsError:
            pass
    return container


def invoice_path(blob_name: str) -> str:
    """Normalize a trigger blob name (``invoices/x.pdf`` or ``x.pdf``) to a path in the container."""
    name = blob_name.strip().lstrip("/")
    prefix = f"{invoices_container_name()}/"
    if name.startswith(prefix):
        name = name[len(prefix):]
    parts = PurePosixPath(name).parts
    if not name or any(part in ("..", ".") for part in parts):
        raise ValueError(f"Invalid invoice blob name: {blob_name!r}")
    return name


def read_invoice(blob_name: str) -> bytes:
    blob = _service().get_blob_client(invoices_container_name(), invoice_path(blob_name))
    size = blob.get_blob_properties().size
    if size > MAX_INVOICE_BYTES:
        raise ValueError(f"Invoice is {size} bytes; the limit is {MAX_INVOICE_BYTES}.")
    return blob.download_blob().readall()


def save_review_once(review_id: str, document: dict[str, Any]) -> bool:
    """Write the review for ``review_id``. Return ``False`` if one already exists."""
    blob = _reviews().get_blob_client(f"{review_id}.json")
    try:
        blob.upload_blob(json.dumps(document, indent=2), overwrite=False, content_type="application/json")
    except ResourceExistsError:
        return False
    return True
