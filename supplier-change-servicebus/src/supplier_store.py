# Copyright (c) Microsoft Corporation.
# Licensed under the MIT License.

"""Conditional recommendation writes and at-least-once buyer notifications."""

from __future__ import annotations

import json
import os
from contextlib import contextmanager
from typing import Any

from azure.core.exceptions import ResourceExistsError, ResourceNotFoundError
from azure.identity import DefaultAzureCredential
from azure.servicebus import ServiceBusClient, ServiceBusMessage
from azure.storage.blob import ContainerClient, ContentSettings

from supplier_process import identifier


@contextmanager
def container_client():
    name = os.environ.get("RECOMMENDATIONS_CONTAINER", "supplier-change-recommendations")
    connection = os.environ.get("AzureWebJobsStorage", "")
    if connection:
        with ContainerClient.from_connection_string(connection, name) as container:
            try:
                container.create_container()
            except ResourceExistsError:
                pass
            yield container
    else:
        with DefaultAzureCredential(managed_identity_client_id=os.environ.get("AZURE_CLIENT_ID")) as identity:
            with ContainerClient(
                os.environ["AzureWebJobsStorage__blobServiceUri"], name, credential=identity,
            ) as container:
                yield container


def read(change_id: str) -> dict[str, Any] | None:
    identifier(change_id, "change_id")
    with container_client() as container:
        try:
            return json.loads(container.download_blob(f"{change_id}.json").readall())
        except ResourceNotFoundError:
            return None


def save_once(record: dict[str, Any]) -> dict[str, Any]:
    change_id = identifier(record["change_id"], "change_id")
    with container_client() as container:
        try:
            container.upload_blob(
                f"{change_id}.json", json.dumps(record, indent=2), overwrite=False,
                content_settings=ContentSettings(content_type="application/json"),
            )
        except ResourceExistsError:
            # A concurrent delivery may have won. Its stored decision is authoritative.
            return json.loads(container.download_blob(f"{change_id}.json").readall())
    return record


def notify_buyer(record: dict[str, Any]) -> None:
    """Send only a reference, never supplier prose or model-generated text.

    Send after persistence on *every* replay of a review record. A failed send
    therefore retries without another model call. A crash after send can produce
    duplicate notifications; downstream consumers must deduplicate by change_id.
    """
    if not record["requires_human_approval"]:
        return
    namespace = os.environ["SUPPLIER_SERVICEBUS_NAMESPACE"]
    notification = {key: record[key] for key in ("change_id", "po_id", "decision")}
    notification["recommendation_blob"] = f"{record['change_id']}.json"
    with DefaultAzureCredential(managed_identity_client_id=os.environ.get("AZURE_CLIENT_ID")) as identity:
        with ServiceBusClient(namespace, identity) as client:
            with client.get_queue_sender("supplier-reviews") as sender:
                sender.send_messages(ServiceBusMessage(
                    json.dumps(notification), message_id=record["change_id"], content_type="application/json",
                ))