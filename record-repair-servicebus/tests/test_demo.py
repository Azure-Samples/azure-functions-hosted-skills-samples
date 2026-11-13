# Copyright (c) Microsoft Corporation.
# Licensed under the MIT License.

from demo import EXPECTED, load_samples, validate, with_fresh_id


def test_every_sample_has_an_expected_outcome_and_an_id():
    samples = load_samples()
    assert set(samples) == set(EXPECTED)
    for record in samples.values():
        record_id, fresh = with_fresh_id(record, "abc123")
        assert record_id.endswith("-abc123")


def test_with_fresh_id_keeps_the_original_field_spelling():
    record_id, fresh = with_fresh_id({"recordId": "rec-renamed", "value": "21.5"}, "x1")
    assert fresh == {"recordId": "rec-renamed-x1", "value": "21.5"}
    assert record_id == "rec-renamed-x1"


def test_validate_reports_each_outcome():
    expected = {"a": "processed", "b": "repaired", "c": "unrepairable", "d": "repaired"}
    processed = {
        "a": {"source": "partner-feed", "value": 21.5, "repaired": False},
        "b": {"source": "partner-feed", "value": 21.5, "repaired": False},
    }
    failures = {"c": {"reason": "value is missing"}}
    assert validate(expected, processed, failures) == [
        "b: expected repaired=True, got False",
        "d: not in processed-data yet",
    ]


def test_validate_fails_if_unrepairable_record_was_ingested():
    errors = validate({"c": "unrepairable"}, {"c": {"source": "x", "value": 0, "repaired": True}}, {})
    assert errors == ["c: expected unrepairable but it was ingested"]
