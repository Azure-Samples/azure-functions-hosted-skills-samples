"""Records a dead-lettered message that could not be repaired, for a person to review.

Writes ``repair-failures/<message id>.json`` once (``overwrite=False``), so a retried
skill run doesn't create a second entry.

Do not add ``from __future__ import annotations`` here: the runtime loads tool files
dynamically and pydantic must resolve the model's types at import time.
"""

import hashlib
import re
from datetime import UTC, datetime
from typing import Any

from ingestion import storage
from pydantic import BaseModel, Field

_SAFE_ID = re.compile(r"^[A-Za-z0-9._-]{1,100}$")
_MAX_BODY = 4000


class UnrepairableRecord(BaseModel):
    message_id: str = Field(description="message_id of the dead-lettered message, unchanged.")
    record_id: str = Field(default="", description="The record's ID if one can be read from the body, otherwise empty.")
    reason: str = Field(description="One or two sentences: why the record can't be repaired without guessing.")
    original_body: str = Field(description="The dead-lettered message body text exactly as received in `body`.")


def record_unrepairable(failure: UnrepairableRecord) -> dict[str, Any]:
    """Save an unrepairable dead-lettered message to the repair-failures container for human review. Call once per message."""
    message_id = failure.message_id.strip()
    blob_id = message_id if _SAFE_ID.match(message_id) else hashlib.sha256(message_id.encode()).hexdigest()[:32]
    document = {
        "message_id": message_id,
        "record_id": failure.record_id.strip(),
        "reason": failure.reason.strip()[:1000],
        "original_body": failure.original_body[:_MAX_BODY],
        "truncated": len(failure.original_body) > _MAX_BODY,
        "recorded_at": datetime.now(UTC).isoformat(),
    }
    container = storage.failures_container_name()
    if not storage.write_once(container, f"{blob_id}.json", document):
        return {"status": "duplicate", "blob": f"{container}/{blob_id}.json"}
    return {"status": "recorded", "blob": f"{container}/{blob_id}.json"}
