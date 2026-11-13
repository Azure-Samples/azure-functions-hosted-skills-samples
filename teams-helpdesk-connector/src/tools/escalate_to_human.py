# Copyright (c) Microsoft Corporation.
# Licensed under the MIT License.

"""Escalates a question to the human IT service desk queue.

The sample records the escalation as ``escalations/<message_id>.json`` in Blob
Storage. In production, replace ``save_once`` with a call to your ITSM system or a
queue that the service desk works from. One escalation per Teams message.

Do not add ``from __future__ import annotations`` here: the runtime loads tool
files dynamically and pydantic must resolve the model's types at import time.
"""

import hashlib
from datetime import datetime, timezone
from typing import Any, Literal

from _storage import check_message_id
from _storage import load as load_record
from _storage import save_once
from pydantic import BaseModel, Field

QUEUE = "it-service-desk"


class Escalation(BaseModel):
    message_id: str = Field(description="The Teams message 'id'. Used as the idempotency key.")
    requester: str = Field(description="Display name or email of the employee who asked.")
    summary: str = Field(description="One or two sentences a service desk analyst can act on.")
    reason: str = Field(description="Why a person is needed, for example 'no knowledge base match' or 'admin rights requested'.")
    priority: Literal["low", "normal", "high"] = Field(
        default="normal", description="high only when the employee cannot work at all or reports a security issue."
    )


def escalate_to_human(escalation: Escalation) -> dict[str, Any]:
    """Hand the question to the human IT service desk queue. Idempotent per Teams message."""
    message_id = check_message_id(escalation.message_id)
    record = {
        "escalation_id": "ESC-" + hashlib.sha256(message_id.encode()).hexdigest()[:8].upper(),
        "queue": QUEUE,
        "message_id": message_id,
        "requester": escalation.requester,
        "summary": escalation.summary,
        "reason": escalation.reason,
        "priority": escalation.priority,
        "status": "open",
        "created_at": datetime.now(timezone.utc).isoformat(),
    }
    if not save_once("escalations", message_id, record):
        existing = load_record("escalations", message_id) or record
        return {"created": False, "duplicate": True, **existing}
    return {"created": True, "duplicate": False, **record}
