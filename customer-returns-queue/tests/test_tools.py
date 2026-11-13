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
    assert result == {"status": "recorded", "request_id": "RR-1001", "requires_human_approval": True}
    assert store["RR-1001"]["requires_human_approval"] is True


def test_record_is_idempotent(store):
    assert record_return_recommendation(_recommendation())["status"] == "recorded"
    assert record_return_recommendation(_recommendation())["status"] == "duplicate"


def test_record_rejects_unsafe_request_id(store):
    assert record_return_recommendation(_recommendation(request_id="../x"))["status"] == "rejected"
    assert store == {}
