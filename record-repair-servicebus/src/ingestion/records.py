"""Record rules shared by the ``ingest`` function and the repair tools.

Everything here is deterministic. The ingest function uses it to accept or reject a record,
and the repair tools use the same checks so a "repaired" record is held to the same contract.
"""

from __future__ import annotations

import json
import re
from datetime import UTC, datetime
from functools import lru_cache
from pathlib import Path
from typing import Any

from jsonschema import Draft202012Validator

SCHEMA_PATH = Path(__file__).resolve().parent.parent / "record-schema.json"
SCHEMA_VERSION = 1
REPAIR_PREFIX = "repair-"


@lru_cache
def schema() -> dict[str, Any]:
    return json.loads(SCHEMA_PATH.read_text(encoding="utf-8"))


@lru_cache
def _validator() -> Draft202012Validator:
    return Draft202012Validator(schema())


def validation_errors(record: Any) -> list[str]:
    """Return human-readable schema errors; an empty list means the record is valid."""
    errors = sorted(_validator().iter_errors(record), key=lambda e: list(e.absolute_path))
    return [f"{'/'.join(map(str, e.absolute_path)) or '(record)'}: {e.message}" for e in errors]


def normalize_source(source: str) -> str:
    """Lowercase slug: ' Partner Feed ' -> 'partner-feed'."""
    return re.sub(r"[^a-z0-9]+", "-", source.lower()).strip("-")


def is_repaired(properties: dict[Any, Any] | None) -> bool:
    """True if Service Bus application properties carry the ``repaired`` marker."""
    for key, value in (properties or {}).items():
        key = key.decode() if isinstance(key, bytes) else str(key)
        if key.lower() == "repaired":
            if isinstance(value, bytes):
                value = value.decode()
            return value is True or str(value).strip().lower() in {"true", "1"}
    return False


def build_output(
    record: dict[str, Any],
    enqueued_time: datetime | None,
    repaired: bool = False,
    repair_changes: str = "",
) -> tuple[str, dict[str, Any]]:
    """Return ``(blob_name, document)`` for a valid record."""
    document = dict(record)
    document["source"] = normalize_source(record["source"])
    document["ingested_at"] = datetime.now(UTC).isoformat()
    document["schema_version"] = SCHEMA_VERSION
    document["repaired"] = repaired
    if repaired and repair_changes:
        document["repair_changes"] = repair_changes
    # The enqueue time keeps the partition stable if a delivery is retried after midnight.
    enqueued_time = enqueued_time or datetime.now(UTC)
    if enqueued_time.tzinfo is None:
        enqueued_time = enqueued_time.replace(tzinfo=UTC)
    partition = enqueued_time.astimezone(UTC).strftime("%Y/%m/%d")
    return f"{document['source']}/{partition}/{record['record_id']}.json", document


def _key(name: str) -> str:
    return re.sub(r"[^a-z0-9]", "", name.lower())


def _same_value(original: Any, corrected: Any) -> bool:
    if original == corrected and type(original) is type(corrected):
        return True
    # A numeric string such as "21.5" may become the number 21.5, and nothing else.
    if isinstance(original, str) and isinstance(corrected, int | float) and not isinstance(corrected, bool):
        try:
            return float(original.strip()) == float(corrected)
        except ValueError:
            return False
    return False


def format_only_problems(original: Any, corrected: dict[str, Any]) -> list[str]:
    """Check that ``corrected`` is ``original`` with only format changes.

    Allowed: renaming a field to a spelling that differs only in case, ``_``, or ``-``
    (``recordId`` -> ``record_id``, ``Value`` -> ``value``), and turning a numeric string into
    a number. Not allowed: adding, dropping, or changing any value. Returns the problems found.
    """
    if not isinstance(original, dict):
        return ["the original body is not a JSON object"]
    originals: dict[str, tuple[str, Any]] = {}
    for name, value in original.items():
        if _key(name) in originals:
            return [f"the original body has ambiguous fields {originals[_key(name)][0]!r} and {name!r}"]
        originals[_key(name)] = (name, value)

    problems = []
    matched = set()
    for name, value in corrected.items():
        source = originals.get(_key(name))
        if source is None:
            problems.append(f"{name!r} is not in the original record (no invented fields)")
            continue
        matched.add(_key(name))
        if not _same_value(source[1], value):
            problems.append(f"{name!r} changed from {source[1]!r} to {value!r} (only numeric strings may become numbers)")
    for key, (name, _) in originals.items():
        if key not in matched:
            problems.append(f"{name!r} from the original record was dropped")
    return problems
