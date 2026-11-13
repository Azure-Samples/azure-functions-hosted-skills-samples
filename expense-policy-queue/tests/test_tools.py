import json
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace

import _storage
import pytest
from azure.core.exceptions import ResourceNotFoundError
from list_policies import list_policies
from read_policy import read_policy
from route_expense_decision import ExpenseDecision, route_expense_decision

TEMPLATE_DIR = Path(__file__).resolve().parent.parent
POLICIES_DIR = TEMPLATE_DIR / "policies"


class FakeBlob:
    def __init__(self, container, name):
        self.container = container
        self.blob_name = name

    def get_blob_properties(self):
        if self.blob_name not in self.container.files:
            raise ResourceNotFoundError("missing")
        return SimpleNamespace(size=len(self.container.files[self.blob_name]), last_modified=datetime(2026, 11, 1, tzinfo=UTC))

    def download_blob(self):
        return SimpleNamespace(readall=lambda: self.container.files[self.blob_name])


class FakeContainer:
    container_name = "policies"

    def __init__(self, files):
        self.files = files

    def list_blobs(self):
        for name, data in self.files.items():
            yield SimpleNamespace(name=name, size=len(data), last_modified=datetime(2026, 11, 1, tzinfo=UTC))

    def download_blob(self, name):
        return FakeBlob(self, name).download_blob()

    def get_blob_client(self, name):
        return FakeBlob(self, name)


@pytest.fixture
def policy_store(monkeypatch):
    files = {p.name: p.read_bytes() for p in POLICIES_DIR.glob("*.md")}
    files["notes.txt"] = b"not a policy"
    monkeypatch.setattr(_storage, "policies", lambda: FakeContainer(files))
    return files


def test_list_policies_returns_names_and_scope(policy_store):
    result = list_policies()
    by_name = {p["name"]: p["applies_to"] for p in result["policies"]}
    assert set(by_name) == {
        "equipment-software-policy.md",
        "general-expense-policy.md",
        "meals-entertainment-policy.md",
        "travel-policy.md",
    }
    assert by_name["travel-policy.md"].startswith("air travel, rail, lodging")
    assert result["fallback"] == "general-expense-policy.md"


def test_read_policy_accepts_prefixed_names(policy_store):
    assert "$1,000 or less" in read_policy("travel-policy.md")["content"]
    assert read_policy("/policies/travel-policy.md")["name"] == "travel-policy.md"


@pytest.mark.parametrize("name", ["../secrets.md", "policies/../x.md", "travel-policy.txt", "a/b.md", "", "notes.txt"])
def test_read_policy_rejects_unsafe_names(policy_store, name):
    assert "error" in read_policy(name)


def test_read_policy_requires_existing_document(policy_store):
    assert "does not exist" in read_policy("lodging-policy.md")["error"]


def test_strict_travel_policy_reroutes_450_usd():
    default = (POLICIES_DIR / "travel-policy.md").read_text(encoding="utf-8")
    strict = (TEMPLATE_DIR / "samples" / "strict-travel-policy.md").read_text(encoding="utf-8")
    assert "| $1,000 or less" in default
    assert "| $250 or less" in strict and "| Over $250, up to $2,500  | `route`" in strict


@pytest.fixture
def router(monkeypatch):
    markers: dict[str, dict] = {}
    sent: list[tuple[str, dict]] = []

    def claim(expense_id, document):
        if expense_id in markers:
            return False
        markers[expense_id] = document
        return True

    def send(queue, message):
        sent.append((queue, json.loads(message)))
        return f"msg-{len(sent)}"

    monkeypatch.setattr(_storage, "claim_decision", claim)
    monkeypatch.setattr(_storage, "release_decision", lambda expense_id: markers.pop(expense_id, None))
    monkeypatch.setattr(_storage, "send_to_queue", send)
    return SimpleNamespace(markers=markers, sent=sent)


def _decision(**overrides):
    values = {
        "expenseId": "EXP-1001",
        "decision": "approve",
        "queue": "expense-approved",
        "amount": 450,
        "currency": "usd",
        "vendor": "United Airlines",
        "category": "travel",
        "policyApplied": "travel-policy.md",
        "reason": "450 USD travel is within the 1,000 USD auto-approve threshold (rule 5).",
    }
    values.update(overrides)
    return ExpenseDecision(**values)


def test_route_sends_compact_decision_once(router):
    assert route_expense_decision(_decision()) == {
        "status": "routed", "expenseId": "EXP-1001", "queue": "expense-approved", "messageId": "msg-1"
    }
    queue, message = router.sent[0]
    assert queue == "expense-approved"
    assert message["currency"] == "USD" and message["routedTo"] == "expense-approved"
    assert route_expense_decision(_decision())["status"] == "duplicate"
    assert len(router.sent) == 1


@pytest.mark.parametrize(
    "overrides, error",
    [
        ({"queue": "expense-review"}, "must go to queue 'expense-approved'"),
        ({"currency": "EUR"}, "Non-USD"),
        ({"amount": None, "reason": "No amount."}, "positive amount"),
        ({"expenseId": "../x"}, "expenseId"),
        ({"policyApplied": "../travel-policy.md"}, "Invalid policy"),
        ({"reason": "  "}, "reason"),
    ],
)
def test_route_enforces_rules_in_code(router, overrides, error):
    result = route_expense_decision(_decision(**overrides))
    assert result["status"] == "rejected"
    assert any(error in e for e in result["errors"])
    assert router.sent == [] and router.markers == {}


def test_route_flags_without_amount_and_reviews_foreign_currency(router):
    flagged = _decision(expenseId="EXP-2", decision="flag", queue="expense-flagged", amount=None, reason="No amount.")
    reviewed = _decision(expenseId="EXP-3", decision="review", queue="expense-review", amount=480, currency="EUR")
    assert route_expense_decision(flagged)["status"] == "routed"
    assert route_expense_decision(reviewed)["status"] == "routed"
    assert [q for q, _ in router.sent] == ["expense-flagged", "expense-review"]


def test_route_releases_marker_when_send_fails(router, monkeypatch):
    def fail(queue, message):
        raise RuntimeError("queue unavailable")

    monkeypatch.setattr(_storage, "send_to_queue", fail)
    assert route_expense_decision(_decision())["status"] == "error"
    assert router.markers == {}


def test_send_to_queue_only_allows_output_queues():
    with pytest.raises(ValueError):
        _storage.send_to_queue("expense-requests", "{}")
