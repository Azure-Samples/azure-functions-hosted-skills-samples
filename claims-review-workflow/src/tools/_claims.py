# Copyright (c) Microsoft Corporation.
# Licensed under the MIT License.

"""Blob Storage access for claim packages and claim decisions.

Uses the ``AzureWebJobsStorage`` connection: a connection string locally (Azurite),
or the ``AzureWebJobsStorage__blobServiceUri`` setting with the app's managed identity
in Azure. Files whose names start with ``_`` are helpers, not tools.
"""

from __future__ import annotations

import json
import os
from functools import cache
from io import BytesIO
from pathlib import PurePosixPath
from typing import Any

from azure.core.exceptions import ResourceExistsError, ResourceNotFoundError
from azure.storage.blob import BlobServiceClient, ContainerClient

MAX_CLAIM_BYTES = 256 * 1024
MAX_ATTACHMENT_BYTES = 5 * 1024 * 1024
MAX_EXCERPT_CHARS = 1500


@cache
def _service() -> BlobServiceClient:
    connection_string = os.environ.get("AzureWebJobsStorage", "")
    if connection_string:
        return BlobServiceClient.from_connection_string(connection_string)

    from azure.identity import DefaultAzureCredential

    credential = DefaultAzureCredential(managed_identity_client_id=os.environ.get("AZURE_CLIENT_ID"))
    return BlobServiceClient(os.environ["AzureWebJobsStorage__blobServiceUri"], credential=credential)


def inbox_container_name() -> str:
    return os.environ.get("CLAIMS_INBOX_CONTAINER", "claims-inbox")


def attachments_container_name() -> str:
    return os.environ.get("CLAIMS_ATTACHMENTS_CONTAINER", "claims-attachments")


def decisions_container_name() -> str:
    return os.environ.get("CLAIMS_DECISIONS_CONTAINER", "claims-decisions")


@cache
def _decisions() -> ContainerClient:
    container = _service().get_container_client(decisions_container_name())
    if os.environ.get("AzureWebJobsStorage"):
        # Local development (Azurite): create the container on first use. In Azure it is
        # provisioned by infra/main.bicep.
        try:
            container.create_container()
        except ResourceExistsError:
            pass
    return container


def safe_path(name: str, container: str = "") -> str:
    """Normalize a blob path and reject traversal. Strips a leading ``<container>/``."""
    if not isinstance(name, str):
        raise ValueError("Blob name must be a string.")
    path = name.strip().replace("\\", "/").lstrip("/")
    if container and path.startswith(f"{container}/"):
        path = path[len(container) + 1:]
    parts = PurePosixPath(path).parts
    if not path or any(part in ("..", ".") for part in parts):
        raise ValueError(f"Invalid blob name: {name!r}")
    return path


def claim_path(blob_name: str) -> str:
    """Normalize a trigger blob name (``claims-inbox/x.json`` or ``x.json``)."""
    return safe_path(blob_name, inbox_container_name())


def decision_id(blob_name: str) -> str:
    """Stable decision id for a claim blob: its path without the ``.json`` suffix."""
    path = claim_path(blob_name)
    stem = path[:-5] if path.lower().endswith(".json") else path
    return stem.replace("/", "__")


def load_claim(blob_name: str) -> dict[str, Any]:
    """Download and parse the claim form. Raises ``ValueError`` if it is not a JSON object."""
    blob = _service().get_blob_client(inbox_container_name(), claim_path(blob_name))
    size = blob.get_blob_properties().size
    if size > MAX_CLAIM_BYTES:
        raise ValueError(f"Claim file is {size} bytes; the limit is {MAX_CLAIM_BYTES}.")
    raw = blob.download_blob().readall()
    try:
        claim = json.loads(raw.decode("utf-8-sig"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError(f"Claim file is not valid JSON: {exc}") from exc
    if not isinstance(claim, dict):
        raise ValueError("Claim file must contain a JSON object.")
    return claim


def attachment_exists(file: str) -> bool:
    try:
        path = safe_path(file, attachments_container_name())
    except ValueError:
        return False
    return _service().get_blob_client(attachments_container_name(), path).exists()


def read_attachment_text(file: str) -> str:
    """Return a short text excerpt of a ``.txt``/``.md``/``.json`` or ``.pdf`` attachment."""
    path = safe_path(file, attachments_container_name())
    blob = _service().get_blob_client(attachments_container_name(), path)
    try:
        size = blob.get_blob_properties().size
    except ResourceNotFoundError as exc:
        raise FileNotFoundError(path) from exc
    if size > MAX_ATTACHMENT_BYTES:
        return f"[attachment is {size} bytes; too large to excerpt]"
    data = blob.download_blob().readall()
    if path.lower().endswith(".pdf"):
        from pypdf import PdfReader

        reader = PdfReader(BytesIO(data))
        text = "\n".join((page.extract_text() or "") for page in reader.pages[:5])
    else:
        text = data.decode("utf-8", errors="replace")
    return text[:MAX_EXCERPT_CHARS]


def save_decision_once(decision_id_: str, document: dict[str, Any]) -> bool:
    """Write the decision record. Return ``False`` if one already exists for this claim blob."""
    blob = _decisions().get_blob_client(f"{decision_id_}.json")
    try:
        blob.upload_blob(json.dumps(document, indent=2), overwrite=False, content_type="application/json")
    except ResourceExistsError:
        return False
    return True


def read_decision(decision_id_: str) -> dict[str, Any] | None:
    blob = _decisions().get_blob_client(f"{decision_id_}.json")
    try:
        return json.loads(blob.download_blob().readall())
    except ResourceNotFoundError:
        return None
