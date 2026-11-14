# Copyright (c) Microsoft Corporation.
# Licensed under the MIT License.

"""Marks a recorded Teams reply as delivered, after the Teams MCP reply tool succeeded.

Writes ``deliveries/<message_id>.json`` with ``overwrite=False``. Until this record
exists, ``post_reply`` returns ``status="pending_delivery"`` for a redelivered message,
so a reply whose Teams call failed (or whose run stopped) is sent again instead of
being suppressed as a duplicate.

Only a message with a recorded reply that is meant to be posted in Teams can be
marked; anything else is rejected.

Do not add ``from __future__ import annotations`` here: the runtime loads tool
files dynamically and pydantic must resolve the model's types at import time.
"""

from datetime import datetime, timezone
from typing import Any

from _storage import check_message_id
from _storage import load as load_record
from _storage import save_once
from pydantic import BaseModel, Field


class Delivery(BaseModel):
    message_id: str = Field(description="The Teams message 'id' whose reply was just posted in Teams.")
    teams_reply_id: str = Field(default="", description="The ID of the posted reply returned by the Teams reply tool, if it returned one.")


def mark_reply_delivered(delivery: Delivery) -> dict[str, Any]:
    """Call only after the Teams reply tool succeeded for this message. Marks the recorded reply as delivered so a redelivered message is not answered again."""
    message_id = check_message_id(delivery.message_id)
    record = load_record("replies", message_id)
    if record is None:
        raise ValueError(f"No reply is recorded for message {message_id!r}. Call post_reply first.")
    if not record.get("deliver_via_teams"):
        raise ValueError(f"The reply for message {message_id!r} is not delivered through Teams; there is nothing to mark.")

    saved = save_once(
        "deliveries",
        message_id,
        {
            "message_id": message_id,
            "teams_reply_id": delivery.teams_reply_id.strip(),
            "delivered_at": datetime.now(timezone.utc).isoformat(),
        },
    )
    return {"status": "delivered" if saved else "already_delivered", "message_id": message_id}
