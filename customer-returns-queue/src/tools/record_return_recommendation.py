"""Records the hosted skill's recommendation for a support agent to review.

This tool only stores a recommendation. Refunds, replacements, and credits stay
in your order system behind its own approval process.

Do not add ``from __future__ import annotations`` here: the runtime loads tool
files dynamically and pydantic must resolve the model's types at import time.
"""

import re
from datetime import UTC, datetime
from typing import Any, Literal

import _recommendation_store
from pydantic import BaseModel, Field

_REQUEST_ID = re.compile(r"^[A-Za-z0-9_-]{1,64}$")


class ReturnRecommendation(BaseModel):
    request_id: str = Field(description="request_id from the queue message, or 'unknown' if it was missing.")
    order_id: str = Field(default="", description="order_id from the queue message.")
    decision: Literal["approve_standard", "approve_exception", "decline", "escalate"]
    resolution: Literal["refund", "replacement", "store_credit", "none"]
    requires_human_approval: bool = Field(
        description="true for every decision except approve_standard."
    )
    reasons: list[str] = Field(description="Short, verifiable reasons tied to the policy or evidence.")
    evidence: list[str] = Field(description="Specific facts used, such as dates, ticket IDs, and policy clause IDs.")
    customer_reply_draft: str = Field(description="Friendly reply a support agent can send after approval.")


def record_return_recommendation(recommendation: ReturnRecommendation) -> dict[str, Any]:
    """Record the recommendation for a return request so a support agent can review it. Call once per request."""
    request_id = recommendation.request_id.strip()
    if not _REQUEST_ID.match(request_id):
        return {"status": "rejected", "error": "request_id must be 1-64 letters, digits, '-' or '_'."}

    # Code enforces the approval rule instead of trusting the model's flag.
    requires_approval = recommendation.decision != "approve_standard" or recommendation.requires_human_approval

    document = recommendation.model_dump()
    document["request_id"] = request_id
    document["requires_human_approval"] = requires_approval
    document["recorded_at"] = datetime.now(UTC).isoformat()

    if not _recommendation_store.save_once(request_id, document):
        return {"status": "duplicate", "request_id": request_id}
    return {
        "status": "recorded",
        "request_id": request_id,
        "requires_human_approval": requires_approval,
    }
