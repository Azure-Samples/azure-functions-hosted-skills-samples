"""Creates a software request ticket for an approved application.

Code-enforced rules:

* Requests are matched against the software catalog; unapproved or unknown software
  is not ticketed.
* Requests for local administrator or elevated rights are refused. Tickets are always
  created with ``access_level="standard"`` and ``admin_rights_granted=False``.
* One ticket per Teams message: the ticket ID is derived from the message ID and the
  record is written with ``overwrite=False``, so redelivery returns the existing ticket.

Do not add ``from __future__ import annotations`` here: the runtime loads tool
files dynamically and pydantic must resolve the model's types at import time.
"""

import hashlib
import re
from datetime import datetime, timezone
from typing import Any

from _sample_data import load
from _storage import check_message_id
from _storage import load as load_record
from _storage import save_once
from pydantic import BaseModel, Field

_ADMIN_PATTERN = re.compile(
    r"\b(local\s+admin\w*|admin(istrator)?\s+(rights|access|privileges?|account)|elevat\w*|sudo|run\s+as\s+admin\w*|privileged)\b",
    re.IGNORECASE,
)


class SoftwareRequest(BaseModel):
    message_id: str = Field(description="The Teams message 'id' this request came from. Used as the idempotency key.")
    requester: str = Field(description="Display name or email of the employee who asked.")
    software: str = Field(description="The application the employee asked for, as they named it.")
    business_justification: str = Field(default="", description="Why the employee needs it, if they said.")
    original_text: str = Field(default="", description="The employee's message text, used to detect requests for admin rights.")


def _ticket_id(message_id: str) -> str:
    return "SR-" + hashlib.sha256(check_message_id(message_id).encode()).hexdigest()[:8].upper()


def _find(software: str) -> dict[str, Any] | None:
    wanted = software.strip().lower()
    for item in load("software_catalog")["software"]:
        names = [item["name"].lower(), *item["aliases"]]
        if any(wanted == name or name in wanted for name in names):
            return item
    return None


def create_software_request(request: SoftwareRequest) -> dict[str, Any]:
    """Create a software request ticket for approved software. Never grants admin rights; refuses requests for elevated access. Idempotent per Teams message."""
    message_id = check_message_id(request.message_id)
    if _ADMIN_PATTERN.search(f"{request.software} {request.original_text}"):
        return {
            "created": False,
            "reason": "admin_rights_not_granted",
            "admin_rights_granted": False,
            "detail": "Local administrator or elevated rights are never granted through the helpdesk. Approved software is installed by IT through Company Portal. Escalate to a person for the Privileged Access process.",
            "escalate": True,
        }

    item = _find(request.software)
    if item is None:
        return {
            "created": False,
            "reason": "not_in_catalog",
            "admin_rights_granted": False,
            "detail": f"'{request.software}' is not in the software catalog. A person must review it.",
            "escalate": True,
        }
    if item["status"] != "approved":
        return {
            "created": False,
            "reason": "not_approved",
            "admin_rights_granted": False,
            "software": item["name"],
            "detail": item["fulfillment"],
            "escalate": False,
        }
    if item["approval"] == "none":
        return {
            "created": False,
            "reason": "self_service",
            "admin_rights_granted": False,
            "software": item["name"],
            "detail": item["fulfillment"],
            "escalate": False,
        }

    ticket = {
        "ticket_id": _ticket_id(message_id),
        "message_id": message_id,
        "requester": request.requester,
        "software_id": item["id"],
        "software": item["name"],
        "business_justification": request.business_justification,
        "approval_required": item["approval"],
        "fulfillment": item["fulfillment"],
        "access_level": "standard",
        "admin_rights_granted": False,
        "status": "pending_approval",
        "created_at": datetime.now(timezone.utc).isoformat(),
    }
    if not save_once("tickets", message_id, ticket):
        existing = load_record("tickets", message_id) or ticket
        return {"created": False, "reason": "duplicate", "escalate": False, **existing}
    return {"created": True, "escalate": False, **ticket}
