# Copyright (c) Microsoft Corporation.
# Licensed under the MIT License.

"""Records the hosted skill's recommendation for a support agent to review.

This tool only stores a recommendation. Refunds, replacements, and credits stay
in your order system behind its own approval process.

Each recommendation is stored once under a record ID:

- a well-formed request uses its ``request_id``;
- a malformed request (invalid JSON, or no ``request_id``/``order_id``) is keyed by
  its trigger message ID, which is distinct per message and stable across redelivery,
  so every malformed message gets its own escalation and a redelivered one is reported
  as a duplicate. An ID made only of letters, digits, ``-`` and ``_`` is stored as
  ``msg-<ID>``. Any other ID (Service Bus ``MessageId`` is a free-form string of up to
  128 characters, such as ``tenant-42:order.1234``) is stored as ``msg_<SHA-256 of ID>``
  so it is always a safe blob name. The original ID is kept in the record.

Do not add ``from __future__ import annotations`` here: the runtime loads tool
files dynamically and pydantic must resolve the model's types at import time.
"""

import hashlib
import re
from datetime import UTC, datetime
from typing import Any, Literal

import _recommendation_store
from pydantic import BaseModel, Field

_REQUEST_ID = re.compile(r"^[A-Za-z0-9_-]{1,64}$")
_SAFE_MESSAGE_ID = re.compile(r"^[A-Za-z0-9_-]{1,128}$")
_MAX_MESSAGE_ID_LENGTH = 128
_FALLBACK_PREFIX = "msg-"
_HASHED_PREFIX = "msg_"
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
            "ID of the trigger message, copied exactly: `id` for a Storage queue trigger, `message_id` for a "
            "Service Bus trigger. Required when request_id or order_id is missing."
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
        if request_id.lower().startswith((_FALLBACK_PREFIX, _HASHED_PREFIX)):
            return None, f"request_id must not start with '{_FALLBACK_PREFIX}' or '{_HASHED_PREFIX}'."
        return request_id, None
    # Malformed request: key it by the trigger message so distinct messages never collapse.
    if (
        not trigger_message_id.strip()
        or len(trigger_message_id) > _MAX_MESSAGE_ID_LENGTH
        or any(ord(c) < 0x20 or ord(c) == 0x7F for c in trigger_message_id)
    ):
        return None, (
            "A malformed request needs trigger_message_id (Storage queue `id` or Service Bus `message_id`), "
            "copied exactly: 1-128 characters with no control characters."
        )
    if _SAFE_MESSAGE_ID.match(trigger_message_id):
        return f"{_FALLBACK_PREFIX}{trigger_message_id}", None
    digest = hashlib.sha256(trigger_message_id.encode("utf-8")).hexdigest()
    return f"{_HASHED_PREFIX}{digest}", None


def record_return_recommendation(recommendation: ReturnRecommendation) -> dict[str, Any]:
    """Record the recommendation for a return request so a support agent can review it. Call once per request."""
    request_id = recommendation.request_id.strip()
    order_id = recommendation.order_id.strip()
    # Not stripped: a Service Bus MessageId is free-form, and the key must match it exactly.
    trigger_message_id = recommendation.trigger_message_id
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
