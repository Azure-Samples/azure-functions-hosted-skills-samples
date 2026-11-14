# Copyright (c) Microsoft Corporation.
# Licensed under the MIT License.

"""Records the helpdesk reply for a Teams message, exactly once.

Every reply is written to ``replies/<message_id>.json`` with ``overwrite=False``.
Recording the reply (the decision) is tracked separately from delivering it in Teams:
``deliveries/<message_id>.json`` is written by ``mark_reply_delivered`` only after the
Teams MCP reply tool succeeded. The result tells the skill what to do:

* ``status="recorded"`` and ``deliver_via_teams=True``: call the Teams MCP reply tool,
  then ``mark_reply_delivered``.
* ``status="pending_delivery"``: the reply was recorded earlier but never delivered
  (the Teams call failed or the run stopped). Send the returned ``reply`` text with the
  Teams MCP reply tool, then ``mark_reply_delivered``. Nothing new is created.
* ``status="duplicate"``: this message was already answered and delivered; do not reply.
* ``deliver_via_teams=False``: the Teams connector is not configured (or this is the
  HTTP test skill), so the blob record is the reply.

Do not add ``from __future__ import annotations`` here: the runtime loads tool
files dynamically and pydantic must resolve the model's types at import time.
"""

import os
from datetime import datetime, timezone
from typing import Any, Literal

from _storage import check_message_id
from _storage import load as load_record
from _storage import save_once
from pydantic import BaseModel, Field


class Reply(BaseModel):
    message_id: str = Field(description="The Teams message 'id' being answered.")
    channel: Literal["teams", "http"] = Field(description="'teams' for connector-triggered messages, 'http' for the test endpoint.")
    outcome: Literal["answered", "ticket_created", "escalated"] = Field(description="How the question was handled.")
    reply: str = Field(description="The reply text posted to the employee.")
    sources: list[str] = Field(default_factory=list, description="Knowledge base source URLs used in the reply.")
    ticket_id: str = Field(default="", description="Software request ticket ID, if one was created.")
    escalation_id: str = Field(default="", description="Escalation ID, if the question was escalated.")


def post_reply(reply: Reply) -> dict[str, Any]:
    """Record the reply for a message (once). Returns whether to deliver it with the Teams reply tool, and which text to send if an earlier delivery never completed."""
    message_id = check_message_id(reply.message_id)
    deliver_via_teams = reply.channel == "teams" and bool(os.environ.get("TEAMS_MCP_SERVER_URL", "").strip())
    record = {
        **reply.model_dump(),
        "message_id": message_id,
        "deliver_via_teams": deliver_via_teams,
        "recorded_at": datetime.now(timezone.utc).isoformat(),
    }
    if save_once("replies", message_id, record):
        return {
            "status": "recorded",
            "deliver_via_teams": deliver_via_teams,
            "blob": f"replies/{message_id}.json",
        }

    existing = load_record("replies", message_id) or {}
    if existing.get("deliver_via_teams") and load_record("deliveries", message_id) is None:
        # Recorded earlier but never confirmed as posted: resend the same reply.
        return {
            "status": "pending_delivery",
            "deliver_via_teams": True,
            "reply": existing.get("reply", ""),
            "outcome": existing.get("outcome", ""),
            "blob": f"replies/{message_id}.json",
        }
    return {"status": "duplicate", "deliver_via_teams": False, "existing_reply": existing.get("reply", "")}
