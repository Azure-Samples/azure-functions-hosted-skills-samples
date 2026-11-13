"""Blob Storage records for legal review tasks and final decisions.

Uses the ``AzureWebJobsStorage`` connection: a connection string locally (Azurite), or
the ``AzureWebJobsStorage__blobServiceUri`` setting with the app's managed identity in
Azure. Writes are keyed by orchestration instance ID and overwrite, so an activity that
Durable Functions retries produces the same record instead of a duplicate.
"""

from __future__ import annotations

import json
import os
from functools import cache
from typing import Any

from azure.core.exceptions import ResourceExistsError, ResourceNotFoundError
from azure.storage.blob import BlobServiceClient, ContainerClient

TASKS_PREFIX = "legal-tasks"
DECISIONS_PREFIX = "decisions"


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
    container = _service().get_container_client(os.environ.get("REVIEWS_CONTAINER", "contract-reviews"))
    if os.environ.get("AzureWebJobsStorage"):
        # Local development (Azurite): create the container on first use. In Azure it is
        # provisioned by infra/main.bicep.
        try:
            container.create_container()
        except ResourceExistsError:
            pass
    return container


def task_path(instance_id: str) -> str:
    return f"{TASKS_PREFIX}/{instance_id}.json"


def decision_path(instance_id: str) -> str:
    return f"{DECISIONS_PREFIX}/{instance_id}.json"


def write_json(path: str, document: dict[str, Any]) -> str:
    _container().upload_blob(path, json.dumps(document, indent=2), overwrite=True, content_type="application/json")
    return path


def read_json(path: str) -> dict[str, Any] | None:
    try:
        return json.loads(_container().download_blob(path).readall())
    except ResourceNotFoundError:
        return None
