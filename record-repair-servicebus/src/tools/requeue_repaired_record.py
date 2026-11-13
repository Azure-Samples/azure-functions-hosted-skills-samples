"""Sends a format-repaired record back through the original ingest function.

The skill never writes output itself. Every safety rule is enforced here in code, whatever
the model asks for:

- A message that was already repaired once (ID starts with ``repair-``) is never requeued,
  so a record can't loop between the queue and the dead-letter queue.
- The corrected record must match ``record-schema.json``.
- The corrected record must be the original with format-only changes: renamed fields
  (case, ``_``, ``-``) and numeric strings turned into numbers. No added, dropped, or changed values.
- The requeued message ID is ``repair-<original id>``. The queue has duplicate detection on,
  so a second send for the same dead-lettered message is dropped by Service Bus.

Do not add ``from __future__ import annotations`` here: the runtime loads tool files
dynamically and pydantic must resolve the model's types at import time.
"""

import json
import logging
import re
from typing import Any

import _servicebus
from ingestion import records
from pydantic import BaseModel, Field

_MESSAGE_ID = re.compile(r"^[A-Za-z0-9._:-]{1,100}$")


class RepairedRecord(BaseModel):
    original_message_id: str = Field(description="message_id of the dead-lettered message, unchanged.")
    correlation_id: str = Field(default="", description="correlation_id of the dead-lettered message, or empty.")
    original_body: str = Field(description="The dead-lettered message body text exactly as received in `body`.")
    corrected_record: dict[str, Any] = Field(
        description="The record with only format fixes applied (field names and numeric strings). Same fields, same values."
    )
    changes: list[str] = Field(description="Each format fix, for example \"renamed recordId to record_id\".")


def _rejected(reason: str, **extra: Any) -> dict[str, Any]:
    return {"status": "rejected", "reason": reason, **extra, "next_step": "call record_unrepairable with this reason"}


async def requeue_repaired_record(repair: RepairedRecord) -> dict[str, Any]:
    """Validate a format-only repair in code and requeue it to the ingestion queue. Call at most once per message."""
    original_id = repair.original_message_id.strip()
    if not _MESSAGE_ID.match(original_id):
        return _rejected("original_message_id must be 1-100 letters, digits, '.', '_', ':' or '-'.")
    if original_id.startswith(records.REPAIR_PREFIX):
        return _rejected("this message was already repaired once; repaired records are never requeued again")

    try:
        original = json.loads(repair.original_body)
    except ValueError:
        return _rejected("original_body is not JSON, so there is nothing to repair the format of")
    if not records.validation_errors(original):
        return _rejected("the original record already matches the schema; its failure was not a format problem")

    schema_errors = records.validation_errors(repair.corrected_record)
    if schema_errors:
        return _rejected("corrected_record does not match the schema", errors=schema_errors)

    problems = records.format_only_problems(original, repair.corrected_record)
    if problems:
        return _rejected("corrected_record is not a format-only repair of the original", problems=problems)

    message_id = f"{records.REPAIR_PREFIX}{original_id}"
    changes = "; ".join(c.strip() for c in repair.changes if c.strip())[:500]
    queue = await _servicebus.send_json(
        repair.corrected_record,
        message_id=message_id,
        correlation_id=repair.correlation_id.strip(),
        application_properties={"repaired": True, "original_message_id": original_id, "repair_changes": changes},
    )
    logging.info("requeued repaired record message_id=%s queue=%s", message_id, queue)
    return {"status": "requeued", "message_id": message_id, "queue": queue, "record_id": repair.corrected_record["record_id"]}
