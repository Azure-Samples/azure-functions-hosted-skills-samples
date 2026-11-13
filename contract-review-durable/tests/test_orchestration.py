import json
from datetime import datetime, timezone
from pathlib import Path

import pytest
from contract_orchestration import APPROVAL_EVENT, AWAITING_APPROVAL, review_process
from contract_process import legal_review_reasons, normalize_approval, policy_from_env, validate_contract

SAMPLES = Path(__file__).resolve().parent.parent / "samples"
POLICY = {"risk_threshold": 40, "approval_timeout_hours": 24.0, "value_threshold": 250_000}
NOW = datetime(2026, 7, 1, 9, 0, tzinfo=timezone.utc)

LOW_RISK_REVIEW = {
    "contract_id": "CTR-2026-0142",
    "risk_level": "low",
    "risk_score": 0,
    "clauses": [{"clause": "payment_terms", "status": "standard", "finding": "Net 45."}],
    "deviations": [],
    "recommendation": "approve",
    "summary": "Matches the playbook.",
}
HIGH_RISK_REVIEW = {
    "contract_id": "CTR-2026-0187",
    "risk_level": "high",
    "risk_score": 100,
    "clauses": [{"clause": "limitation_of_liability", "status": "non_standard", "finding": "3-month cap."}],
    "deviations": [{"clause": "limitation_of_liability", "severity": "high", "contract_position": "3 months",
                    "playbook_position": "12 months", "suggested_redline": "..."}],
    "recommendation": "legal_review",
    "summary": "High risk.",
}


def sample(name):
    return json.loads(next(SAMPLES.glob(f"{name}*.json")).read_text(encoding="utf-8"))


# --- validation and routing --------------------------------------------------------


def test_samples_validate_as_expected():
    assert validate_contract(sample("CTR-2026-0142"))["valid"]
    assert validate_contract(sample("CTR-2026-0187"))["valid"]
    result = validate_contract(sample("CTR-2026-0203"))
    assert not result["valid"]
    assert len(result["errors"]) >= 5


def test_validation_rejects_non_objects_and_unsafe_ids():
    assert not validate_contract("not a contract")["valid"]
    contract = {**sample("CTR-2026-0142"), "contract_id": "../etc/passwd"}
    assert any("contract_id" in e for e in validate_contract(contract)["errors"])


def test_low_risk_review_is_auto_approved():
    contract = validate_contract(sample("CTR-2026-0142"))["contract"]
    assert legal_review_reasons(contract, LOW_RISK_REVIEW, POLICY) == []


def test_any_signal_routes_to_legal():
    contract = validate_contract(sample("CTR-2026-0142"))["contract"]
    assert legal_review_reasons(contract, {**LOW_RISK_REVIEW, "risk_score": 40}, POLICY)
    assert legal_review_reasons(contract, {**LOW_RISK_REVIEW, "recommendation": "legal_review"}, POLICY)
    flagged = {**LOW_RISK_REVIEW, "clauses": [{"clause": "governing_law", "status": "missing", "finding": ""}]}
    assert "governing_law" in legal_review_reasons(contract, flagged, POLICY)[0]
    # Value threshold is enforced in code even when the model sees no risk.
    assert legal_review_reasons({**contract, "annual_value": 300_000}, LOW_RISK_REVIEW, POLICY)


def test_unclear_approval_is_a_rejection():
    assert normalize_approval({"decision": "approved", "approver": "a@contoso.com"})["decision"] == "approved"
    assert normalize_approval({"decision": "yes please"})["decision"] == "rejected"
    assert normalize_approval(None) == {"decision": "rejected", "approver": "unknown", "comment": ""}


def test_policy_from_env(monkeypatch):
    monkeypatch.setenv("RISK_THRESHOLD", "55")
    monkeypatch.setenv("APPROVAL_TIMEOUT_HOURS", "0.5")
    policy = policy_from_env()
    assert policy["risk_threshold"] == 55
    assert policy["approval_timeout_hours"] == 0.5


# --- orchestrator, driven with a fake Durable context --------------------------------


class FakeTask:
    def __init__(self, kind, name, payload=None):
        self.kind, self.name, self.payload, self.result = kind, name, payload, None


class FakeContext:
    instance_id = "abc123"
    current_utc_datetime = NOW

    def __init__(self, contract):
        self._input = {"contract": contract, "policy": POLICY}
        self.statuses = []

    def get_input(self):
        return self._input

    def set_custom_status(self, status):
        self.statuses.append(status)

    def call_activity(self, name, input_=None):
        return FakeTask("activity", name, input_)

    def call_activity_with_retry(self, name, retry, input_=None):
        return FakeTask("activity", name, input_)

    def wait_for_external_event(self, name):
        return FakeTask("event", name)

    def create_timer(self, fire_at):
        return FakeTask("timer", "timer", fire_at)

    def task_any(self, tasks):
        return FakeTask("any", "any", tasks)


def run(context, respond):
    """Drive the orchestrator generator; ``respond`` returns the result of each yielded task."""
    calls = []
    gen = review_process(context)
    result = None
    try:
        while True:
            task = gen.send(result)
            calls.append(task.name)
            result = respond(task)
    except StopIteration as stop:
        return stop.value, calls


def activities(review, approval=None):
    def respond(task):
        if task.name == "validate_contract":
            return validate_contract(task.payload)
        if task.name == "review_contract":
            return review
        if task.name == "create_legal_review_task":
            return f"legal-tasks/{task.payload['instance_id']}.json"
        if task.name == "record_decision":
            return f"decisions/{task.payload['instance_id']}.json"
        if task.name == "any":
            event, timer = task.payload
            if approval is None:
                return timer
            event.result = approval
            return event
        raise AssertionError(task.name)
    return respond


def test_low_risk_contract_is_auto_approved():
    context = FakeContext(sample("CTR-2026-0142"))
    decision, calls = run(context, activities(LOW_RISK_REVIEW))
    assert calls == ["validate_contract", "review_contract", "record_decision"]
    assert decision["outcome"] == "auto_approved"
    assert decision["decided_by"] == "policy"
    assert decision["record"] == "decisions/abc123.json"
    assert context.statuses[-1]["stage"] == "completed"


def test_high_risk_contract_waits_for_legal_approval():
    context = FakeContext(sample("CTR-2026-0187"))
    approval = {"decision": "approved", "approver": "legal@contoso.com", "comment": "OK with redlines"}
    decision, calls = run(context, activities(HIGH_RISK_REVIEW, approval))
    assert calls == ["validate_contract", "review_contract", "create_legal_review_task", "any", "record_decision"]
    waiting = next(s for s in context.statuses if s["stage"] == AWAITING_APPROVAL)
    assert waiting["task"] == "legal-tasks/abc123.json"
    assert waiting["due_at"] == "2026-07-02T09:00:00+00:00"
    assert decision["outcome"] == "approved"
    assert decision["approval"]["approver"] == "legal@contoso.com"
    assert decision["deviations"]


def test_legal_rejection_is_recorded():
    context = FakeContext(sample("CTR-2026-0187"))
    decision, _ = run(context, activities(HIGH_RISK_REVIEW, {"decision": "rejected", "approver": "legal@contoso.com"}))
    assert decision["outcome"] == "rejected"


def test_no_decision_before_timeout_escalates():
    context = FakeContext(sample("CTR-2026-0187"))
    decision, _ = run(context, activities(HIGH_RISK_REVIEW, approval=None))
    assert decision["outcome"] == "escalated"
    assert decision["decided_by"] == "timeout"


def test_invalid_contract_never_reaches_the_model():
    context = FakeContext(sample("CTR-2026-0203"))
    decision, calls = run(context, activities(LOW_RISK_REVIEW))
    assert calls == ["validate_contract", "record_decision"]
    assert decision["outcome"] == "invalid"
    assert decision["validation_errors"]


def test_event_name_matches_http_api():
    assert APPROVAL_EVENT == "LegalApproval"


@pytest.mark.parametrize("name", ["CTR-2026-0142", "CTR-2026-0187"])
def test_review_prompt_contains_contract(name):
    from contract_process import review_prompt

    assert sample(name)["contract_id"] in review_prompt(sample(name))
