# Copyright (c) Microsoft Corporation.
# Licensed under the MIT License.

"""Sends repaired records back to the ingestion queue with the Service Bus SDK.

Uses the same ``ServiceBusConnection`` settings as the triggers: the managed identity in
Azure (``AZURE_CLIENT_ID``), or your ``az login`` identity locally.
"""

from __future__ import annotations

import json
import os
from typing import Any


async def send_json(
    body: dict[str, Any],
    message_id: str,
    correlation_id: str,
    application_properties: dict[str, Any],
) -> str:
    """Send ``body`` to the ingestion queue and return the queue name."""
    from azure.identity.aio import DefaultAzureCredential
    from azure.servicebus import ServiceBusMessage
    from azure.servicebus.aio import ServiceBusClient

    namespace = os.environ["ServiceBusConnection__fullyQualifiedNamespace"]
    queue_name = os.environ.get("INGEST_QUEUE", "records")
    message = ServiceBusMessage(
        json.dumps(body, separators=(",", ":"), allow_nan=False),
        message_id=message_id,
        correlation_id=correlation_id or None,
        content_type="application/json",
        application_properties=application_properties,
    )
    async with DefaultAzureCredential(managed_identity_client_id=os.environ.get("AZURE_CLIENT_ID")) as credential:
        async with ServiceBusClient(namespace, credential) as client:
            async with client.get_queue_sender(queue_name) as sender:
                await sender.send_messages(message)
    return queue_name
