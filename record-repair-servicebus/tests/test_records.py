# Copyright (c) Microsoft Corporation.
# Licensed under the MIT License.

import json
from datetime import UTC, datetime
from pathlib import Path

from ingestion import records

SAMPLES_DIR = Path(__file__).resolve().parent.parent / "samples"


def _sample(name):
    return json.loads((SAMPLES_DIR / f"{name}.json").read_text(encoding="utf-8"))


def test_only_the_valid_sample_matches_the_schema():
    assert records.validation_errors(_sample("01-valid")) == []
    for name in ("02-renamed-field", "03-wrong-casing", "04-missing-value"):
        assert records.validation_errors(_sample(name)), name


def test_build_output_normalizes_and_partitions():
    blob, doc = records.build_output(_sample("01-valid"), datetime(2026, 11, 12, 23, 59, tzinfo=UTC))
    assert blob == "partner-feed/2026/11/12/rec-valid.json"
    assert doc["source"] == "partner-feed"
    assert doc["schema_version"] == 1
    assert doc["repaired"] is False
    assert "ingested_at" in doc


def test_build_output_marks_repaired_records():
    _, doc = records.build_output(_sample("01-valid"), None, repaired=True, repair_changes="renamed recordId")
    assert doc["repaired"] is True
    assert doc["repair_changes"] == "renamed recordId"


def test_is_repaired_accepts_bool_string_and_bytes():
    assert records.is_repaired({"repaired": True})
    assert records.is_repaired({"Repaired": "True"})
    assert records.is_repaired({b"repaired": b"true"})
    assert not records.is_repaired({"repaired": False})
    assert not records.is_repaired(None)


def test_format_only_allows_renames_and_numeric_strings():
    original = _sample("02-renamed-field")
    corrected = {"record_id": "rec-renamed", "source": " Partner Feed ", "value": 21.5}
    assert records.format_only_problems(original, corrected) == []
    assert records.format_only_problems(_sample("03-wrong-casing"), {"record_id": "rec-casing", "source": "Field Sensors", "value": 18}) == []


def test_format_only_rejects_invented_changed_and_dropped_data():
    original = _sample("04-missing-value")
    problems = records.format_only_problems(original, {"record_id": "rec-missing", "source": "Field Sensors", "value": 0})
    assert problems == ["'value' is not in the original record (no invented fields)"]

    original = {"record_id": "r1", "source": "a", "value": "21.5"}
    assert records.format_only_problems(original, {"record_id": "r1", "source": "a", "value": 22})
    assert records.format_only_problems(original, {"record_id": "r1", "source": "b", "value": 21.5})
    assert records.format_only_problems({**original, "unit": "C"}, {"record_id": "r1", "source": "a", "value": 21.5})
    assert records.format_only_problems(["not", "an", "object"], {"record_id": "r1"})
