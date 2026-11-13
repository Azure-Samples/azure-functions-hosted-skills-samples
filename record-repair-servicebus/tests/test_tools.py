# Copyright (c) Microsoft Corporation.
# Licensed under the MIT License.

import asyncio
import json

import _servicebus
import pytest
from get_record_schema import get_record_schema
from ingestion import storage
from record_unrepairable import UnrepairableRecord, record_unrepairable
from requeue_repaired_record import RepairedRecord, requeue_repaired_record


@pytest.fixture
def sent(monkeypatch):
    messages = []

    async def send_json(body, message_id, correlation_id, application_properties):
        messages.append({"body": body, "message_id": message_id, "correlation_id": correlation_id,
                         "application_properties": application_properties})
        return "records"

    monkeypatch.setattr(_servicebus, "send_json", send_json)
    return messages


@pytest.fixture
def blobs(monkeypatch):
    saved = {}

    def write_once(container, name, document):
        if (container, name) in saved:
            return False
        saved[(container, name)] = document
        return True

    monkeypatch.setattr(storage, "write_once", write_once)
    return saved


def _repair(**overrides):
    values = {
        "original_message_id": "rec-renamed-abc",
        "correlation_id": "corr-1",
        "original_body": json.dumps({"recordId": "rec-renamed-abc", "source": " Partner Feed ", "value": "21.5"}),
        "corrected_record": {"record_id": "rec-renamed-abc", "source": " Partner Feed ", "value": 21.5},
        "changes": ["renamed recordId to record_id", "converted value '21.5' to 21.5"],
    }
    values.update(overrides)
    return RepairedRecord(**values)


def _requeue(repair):
    return asyncio.run(requeue_repaired_record(repair))


def test_get_record_schema_returns_bundled_schema():
    schema = get_record_schema()
    assert schema["required"] == ["record_id", "source", "value"]
    assert schema["additionalProperties"] is False


def test_requeue_sends_with_repair_id_and_marker(sent):
    result = _requeue(_repair())
    assert result == {"status": "requeued", "message_id": "repair-rec-renamed-abc", "queue": "records",
                      "record_id": "rec-renamed-abc"}
    assert sent[0]["message_id"] == "repair-rec-renamed-abc"
    assert sent[0]["correlation_id"] == "corr-1"
    assert sent[0]["application_properties"]["repaired"] is True
    assert sent[0]["body"]["value"] == 21.5


def test_requeue_refuses_already_repaired_message(sent):
    result = _requeue(_repair(original_message_id="repair-rec-renamed-abc"))
    assert result["status"] == "rejected"
    assert sent == []


def test_requeue_refuses_record_that_fails_schema(sent):
    result = _requeue(_repair(corrected_record={"record_id": "rec-renamed-abc", "source": " Partner Feed ", "value": "21.5"}))
    assert result["status"] == "rejected"
    assert result["errors"]
    assert sent == []


def test_requeue_refuses_invented_value(sent):
    body = json.dumps({"record_id": "rec-missing-abc", "source": "Field Sensors"})
    result = _requeue(_repair(original_message_id="rec-missing-abc", original_body=body,
                              corrected_record={"record_id": "rec-missing-abc", "source": "Field Sensors", "value": 0}))
    assert result["status"] == "rejected"
    assert "no invented fields" in result["problems"][0]
    assert sent == []


def test_requeue_refuses_changed_value(sent):
    result = _requeue(_repair(corrected_record={"record_id": "rec-renamed-abc", "source": " Partner Feed ", "value": 22}))
    assert result["status"] == "rejected"
    assert sent == []


def test_requeue_refuses_when_original_was_already_valid(sent):
    body = json.dumps({"record_id": "rec-valid-abc", "source": "x", "value": 1})
    result = _requeue(_repair(original_message_id="rec-valid-abc", original_body=body,
                              corrected_record={"record_id": "rec-valid-abc", "source": "x", "value": 1}))
    assert result["status"] == "rejected"
    assert sent == []


def test_record_unrepairable_is_idempotent(blobs):
    failure = UnrepairableRecord(message_id="rec-missing-abc", record_id="rec-missing-abc",
                                 reason="value is missing", original_body='{"record_id": "rec-missing-abc"}')
    assert record_unrepairable(failure) == {"status": "recorded", "blob": "repair-failures/rec-missing-abc.json"}
    assert record_unrepairable(failure)["status"] == "duplicate"
    doc = blobs[("repair-failures", "rec-missing-abc.json")]
    assert doc["reason"] == "value is missing"


def test_record_unrepairable_hashes_unsafe_message_id(blobs):
    result = record_unrepairable(UnrepairableRecord(message_id="../../x", reason="bad", original_body="x"))
    assert result["status"] == "recorded"
    assert ".." not in result["blob"]
