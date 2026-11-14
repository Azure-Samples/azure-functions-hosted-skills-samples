# Copyright (c) Microsoft Corporation.
# Licensed under the MIT License.

"""Records the hosted skill's recommendation for a support agent to review.

This tool only stores a recommendation. Refunds, replacements, and credits stay
in your order system behind its own approval process.

Each recommendation is stored once under a record ID:

- a well-formed request uses its ``request_id``;
- a malformed request (invalid JSON, or no ``request_id``/``order_id``) uses
  ``msg-<trigger message ID>``. The trigger message ID is distinct per message and
  stable across redelivery, so every malformed message gets its own escalation and
  a redelivered one is reported as a duplicate.

Do not add ``from __future__ import annotations`` here: the runtime loads tool
files dynamically and pydantic must resolve the model's types at import time.
"""

import re
from datetime import UTC, datetime
from typing import Any, Literal

import _recommendation_store
from pydantic import BaseModel, Field

_REQUEST_ID = re.compile(r"^[A-Za-z0-9_-]{1,64}$")
_TRIGGER_MESSAGE_ID = re.compile(r"^[A-Za-z0-9_-]{1,128}$")
_FALLBACK_PREFIX = "msg-"
_PLACEHOLDER_IDS = {"unknown", "none", "null", "n/a", "missing"}


class ReturnRecommendation(BaseModel):
    request_id: str = Field(
        default="",
        description="request_id from the queue message. Leave empty if the message is malformed.",
    )
    order_id: str = Field(default="", description="order_id from the queue message. Empty if missing.")
    trigger_message_id: str = Field(
        default="",
        description=(
            "ID of the trigger message: `id` for a Storage queue trigger, `message_id` for a Service Bus "
            "trigger. Required when request_id or order_id is missing."
        ),
    )
    decision: Literal["approve_standard", "approve_exception", "decline", "escalate"]
    resolution: Literal["refund", "replacement", "store_credit", "none"]
    requires_human_approval: bool = Field(
        description="true for every decision except approve_standard."
    )
    reasons: list[str] = Field(description="Short, verifiable reasons tied to the policy or evidence.")
    evidence: list[str] = Field(description="Specific facts used, such as dates, ticket IDs, and policy clause IDs.")
    customer_reply_draft: str = Field(description="Friendly reply a support agent can send after approval.")


def _record_id(request_id: str, order_id: str, trigger_message_id: str) -> tuple[str | None, str | None]:
    """Return ``(record_id, error)`` for the recommendation's idempotency key."""
    if request_id.lower() in _PLACEHOLDER_IDS:
        return None, "request_id must be the real ID from the message; leave it empty if it is missing."
    if request_id and order_id:
        if not _REQUEST_ID.match(request_id):
            return None, "request_id must be 1-64 letters, digits, '-' or '_'."
        if request_id.lower().startswith(_FALLBACK_PREFIX):
            return None, f"request_id must not start with '{_FALLBACK_PREFIX}'."
        return request_id, None
    # Malformed request: key it by the trigger message so distinct messages never collapse.
    if not _TRIGGER_MESSAGE_ID.match(trigger_message_id):
        return None, (
            "A malformed request needs trigger_message_id (Storage queue `id` or Service Bus `message_id`): "
            "1-128 letters, digits, '-' or '_'."
        )
    return f"{_FALLBACK_PREFIX}{trigger_message_id}", None


def record_return_recommendation(recommendation: ReturnRecommendation) -> dict[str, Any]:
    """Record the recommendation for a return request so a support agent can review it. Call once per request."""
    request_id = recommendation.request_id.strip()
    order_id = recommendation.order_id.strip()
    trigger_message_id = recommendation.trigger_message_id.strip()
    record_id, error = _record_id(request_id, order_id, trigger_message_id)
    if record_id is None:
        return {"status": "rejected", "error": error}

    document = recommendation.model_dump()
    document.update(
        record_id=record_id,
        request_id=request_id,
        order_id=order_id,
        trigger_message_id=trigger_message_id,
    )
    malformed = record_id != request_id
    if malformed:
        # Code, not the model, decides how malformed requests are handled.
        document["decision"] = "escalate"
        document["resolution"] = "none"

    # Code enforces the approval rule instead of trusting the model's flag.
    requires_approval = document["decision"] != "approve_standard" or recommendation.requires_human_approval
    document["requires_human_approval"] = requires_approval
    document["recorded_at"] = datetime.now(UTC).isoformat()

    if not _recommendation_store.save_once(record_id, document):
        return {"status": "duplicate", "record_id": record_id}
    return {
        "status": "recorded",
        "record_id": record_id,
        "decision": document["decision"],
        "requires_human_approval": requires_approval,
    }
