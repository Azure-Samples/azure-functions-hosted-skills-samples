# Copyright (c) Microsoft Corporation.
# Licensed under the MIT License.

"""Loads the full alert group when the router offloaded it to Blob Storage.

Storage Queue messages are limited to 64 KiB, so the router writes a large group to
``{PAYLOADS_CONTAINER}/{INVESTIGATE_QUEUE}/{group_id}.json`` and queues a small pointer
with ``payload_blob``. This tool reads that blob. The path is built in code from a
validated ``group_id``, so the model can't read anything outside the investigate payloads.

The blob keeps every byte. Very long strings and lists are shortened in the tool result
only, with a marker, so a single huge field can't flood the model's context.
"""

import json
import os
import re
from typing import Any

import _storage

_GROUP_ID = re.compile(r"^[A-Za-z0-9_.:-]{1,80}$")
MAX_STRING = 4000
MAX_ITEMS = 50


def _shorten(value: Any) -> Any:
    if isinstance(value, str) and len(value) > MAX_STRING:
        return f"{value[:MAX_STRING]}...[truncated {len(value) - MAX_STRING} more characters]"
    if isinstance(value, list):
        items = [_shorten(v) for v in value[:MAX_ITEMS]]
        if len(value) > MAX_ITEMS:
            items.append(f"...[truncated {len(value) - MAX_ITEMS} more items]")
        return items
    if isinstance(value, dict):
        return {k: _shorten(v) for k, v in value.items()}
    return value


def get_alert_payload(group_id: str) -> dict[str, Any]:
    """Load the full alert group for a queue message that has ``payload_blob`` instead of ``alerts``.

    Args:
        group_id: ``group_id`` from the queue message.
    """
    group_id = group_id.strip()
    if not _GROUP_ID.match(group_id):
        return {"error": "group_id must be 1-80 letters, digits, '.', ':', '-' or '_'."}
    blob_name = f"{os.environ.get('INVESTIGATE_QUEUE', 'alerts-investigate')}/{group_id}.json"
    text = _storage.load_payload(blob_name)
    if text is None:
        return {"error": f"No offloaded payload for {group_id}.", "group_id": group_id}
    return {"payload_blob": blob_name, **_shorten(json.loads(text))}
