# Copyright (c) Microsoft Corporation.
# Licensed under the MIT License.

import json
from pathlib import Path

import _recommendation_store
import pytest
from get_order import get_order
from get_return_policy import get_return_policy
from get_support_history import get_support_history
from record_return_recommendation import ReturnRecommendation, record_return_recommendation

SAMPLES_DIR = Path(__file__).resolve().parent.parent / "samples"


def test_get_order_calculates_days_since_delivery():
    order = get_order("ORD-5001", as_of="2026-11-09T10:15:00Z")
    assert order["customer_id"] == "C-1001"
    assert order["days_since_delivery"] == 48


def test_get_order_unknown_returns_error():
    assert "error" in get_order("ORD-0000")


def test_get_return_policy_known_and_unknown_category():
    assert get_return_policy("outdoor_gear")["return_window_days"] == 30
    assert "known_categories" in get_return_policy("groceries")


def test_get_support_history():
    assert get_support_history("c-1001")["tickets"][0]["ticket_id"] == "T-2207"
    assert "error" in get_support_history("C-9999")


def test_samples_reference_existing_records():
    for path in SAMPLES_DIR.glob("*.json"):
        sample = json.loads(path.read_text(encoding="utf-8"))
        order = get_order(sample["order_id"], as_of=sample["received_at"])
        assert "error" not in order, path.name
        assert order["customer_id"] == sample["customer_id"], path.name
        for item in order["items"]:
            assert "error" not in get_return_policy(item["category"]), path.name


@pytest.fixture
def store(monkeypatch):
    saved: dict[str, dict] = {}

    def save_once(request_id, document):
        if request_id in saved:
            return False
        saved[request_id] = document
        return True

    monkeypatch.setattr(_recommendation_store, "save_once", save_once)
    return saved


def _recommendation(**overrides):
    values = {
        "request_id": "RR-1001",
        "order_id": "ORD-5001",
        "decision": "approve_exception",
        "resolution": "replacement",
        "requires_human_approval": False,
        "reasons": ["Damage reported within 7 days (OG-EX-1)."],
        "evidence": ["T-2207 opened 2026-09-24"],
        "customer_reply_draft": "Thanks for your patience.",
    }
    values.update(overrides)
    return ReturnRecommendation(**values)


def test_record_enforces_human_approval_for_non_standard_decisions(store):
    result = record_return_recommendation(_recommendation())
    assert result == {
        "status": "recorded",
        "record_id": "RR-1001",
        "decision": "approve_exception",
        "requires_human_approval": True,
    }
    assert store["RR-1001"]["requires_human_approval"] is True


def test_record_is_idempotent(store):
    assert record_return_recommendation(_recommendation())["status"] == "recorded"
    assert record_return_recommendation(_recommendation())["status"] == "duplicate"


def test_record_rejects_unsafe_request_id(store):
    assert record_return_recommendation(_recommendation(request_id="../x"))["status"] == "rejected"
    assert record_return_recommendation(_recommendation(request_id="msg-abc"))["status"] == "rejected"
    assert store == {}


def _malformed(trigger_message_id, **overrides):
    values = {
        "request_id": "",
        "order_id": "",
        "trigger_message_id": trigger_message_id,
        "decision": "escalate",
        "resolution": "none",
        "requires_human_approval": True,
        "reasons": ["The message is not valid JSON."],
        "evidence": [],
        "customer_reply_draft": "We could not read your request.",
    }
    values.update(overrides)
    return _recommendation(**values)


def test_distinct_malformed_messages_get_distinct_escalations(store):
    first = record_return_recommendation(_malformed("6f1c2a7e-0001"))
    second = record_return_recommendation(_malformed("6f1c2a7e-0002", request_id="RR-2001"))
    assert first["status"] == second["status"] == "recorded"
    assert set(store) == {"msg-6f1c2a7e-0001", "msg-6f1c2a7e-0002"}
    assert all(doc["decision"] == "escalate" and doc["requires_human_approval"] for doc in store.values())
    assert store["msg-6f1c2a7e-0002"]["request_id"] == "RR-2001"


def test_redelivered_malformed_message_is_duplicate(store):
    assert record_return_recommendation(_malformed("6f1c2a7e-0001"))["status"] == "recorded"
    assert record_return_recommendation(_malformed("6f1c2a7e-0001")) == {
        "status": "duplicate",
        "record_id": "msg-6f1c2a7e-0001",
    }


def test_malformed_message_is_always_escalated(store):
    result = record_return_recommendation(
        _malformed("abc", decision="approve_standard", resolution="refund", requires_human_approval=False)
    )
    assert result["decision"] == "escalate"
    assert store["msg-abc"]["resolution"] == "none"
    assert store["msg-abc"]["requires_human_approval"] is True


@pytest.mark.parametrize("request_id", ["unknown", "UNKNOWN", " Unknown "])
def test_placeholder_request_id_is_rejected(store, request_id):
    assert record_return_recommendation(_malformed("abc", request_id=request_id))["status"] == "rejected"
    assert store == {}


@pytest.mark.parametrize("trigger_message_id", ["", "  ", "../x", "a" * 129])
def test_malformed_message_requires_valid_trigger_message_id(store, trigger_message_id):
    assert record_return_recommendation(_malformed(trigger_message_id))["status"] == "rejected"
    assert store == {}
