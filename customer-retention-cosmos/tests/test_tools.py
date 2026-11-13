from datetime import date

import _cosmos
import pytest
from _policy import churn_signals, evaluate_offer
from assess_churn_signals import assess_churn_signals
from demo import apply_usage_change, baseline_accounts
from get_account_profile import get_account_profile
from get_plan_catalog import get_plan_catalog
from get_support_tickets import get_support_tickets
from record_retention_action import RetentionAction, record_retention_action

TODAY = date(2026, 11, 12)
DROP = {
    "ACC-1001": {"active_users_30d": 128, "weekly_sessions": 1350},
    "ACC-1002": {"active_users_30d": 44, "weekly_sessions": 310},
    "ACC-1003": {"active_users_30d": 11, "weekly_sessions": 140},
}


@pytest.fixture
def accounts():
    return {d["id"]: d for d in baseline_accounts(TODAY, "baseline-1")}


@pytest.fixture
def cosmos(monkeypatch, accounts):
    """Replace Cosmos DB with in-memory dictionaries."""
    actions: dict[str, dict] = {}

    def create_action_once(document):
        if document["id"] in actions:
            return False
        actions[document["id"]] = document
        return True

    monkeypatch.setattr(_cosmos, "get_account", lambda account_id: accounts.get(account_id))
    monkeypatch.setattr(_cosmos, "create_action_once", create_action_once)
    return actions


def _signals(accounts, account_id, dropped=True):
    doc = accounts[account_id]
    if dropped:
        doc = apply_usage_change(doc, DROP[account_id], "usage-1", TODAY)
    from _sample_data import load

    return churn_signals(doc, load("support_tickets")[account_id], TODAY)


def test_baseline_accounts_need_no_action(accounts):
    for account_id in accounts:
        assert _signals(accounts, account_id, dropped=False)["action_recommended"] is False, account_id


def test_usage_drop_signals_are_computed_in_code(accounts):
    fabrikam = _signals(accounts, "ACC-1001")
    assert fabrikam["usage_change_percent"] == -63.9
    assert fabrikam["seat_utilization_percent"] == 32.0
    assert fabrikam["days_to_renewal"] == 45
    assert fabrikam["open_high_severity_tickets"] == ["SUP-7712"]
    assert (fabrikam["risk_level"], fabrikam["action_recommended"]) == ("high", True)

    northwind = _signals(accounts, "ACC-1002")
    assert (northwind["usage_change_percent"], northwind["days_to_renewal"]) == (-37.1, 21)
    assert (northwind["risk_level"], northwind["action_recommended"]) == ("high", True)

    tailspin = _signals(accounts, "ACC-1003")
    assert (tailspin["usage_change_percent"], tailspin["action_recommended"]) == (-8.3, False)


def test_offer_policy_limits_and_approval():
    assert evaluate_offer("business", 28800, "discount", 16)["allowed"] is False
    assert evaluate_offer("starter", 3600, "discount", 5)["needs_approval"] is False
    business = evaluate_offer("business", 28800, "discount", 12)
    assert (business["offer_value"], business["needs_approval"]) == (3456.0, True)
    enterprise = evaluate_offer("enterprise", 192000, "discount", 6)
    assert (enterprise["offer_value"], enterprise["needs_approval"]) == (11520.0, True)
    assert evaluate_offer("enterprise", 192000, "success_package", 0)["needs_approval"] is False
    assert evaluate_offer("business", 28800, "success_package", 5)["allowed"] is False


def test_lookups(cosmos):
    profile = get_account_profile("acc-1002")
    assert profile["tier"] == "business" and len(profile["usage_history"]["monthly_active_users"]) == 6
    assert "error" in get_account_profile("ACC-9999")
    assert get_support_tickets("ACC-1001")["tickets"][0]["ticket_id"] == "SUP-7712"
    assert get_plan_catalog("Enterprise")["retention_offers"]["max_discount_percent"] == 20
    assert "error" in get_plan_catalog("platinum")
    assert assess_churn_signals("ACC-1004")["usage_snapshot_id"] == "baseline-1"


def _action(**overrides):
    values = {
        "account_id": "ACC-1002",
        "decision": "retention_offer",
        "risk_level": "high",
        "churn_signals": ["Active users down 37.1%."],
        "likely_cause": "Comparing prices before renewal.",
        "offer_type": "discount",
        "discount_percent": 12,
        "offer_summary": "12% off the renewal.",
        "email_subject": "Your Contoso Analytics renewal",
        "email_body": "Hi Ana, ...",
    }
    values.update(overrides)
    return RetentionAction(**values)


def test_record_enforces_policy_and_approval(cosmos):
    assert record_retention_action(_action(discount_percent=20))["status"] == "rejected_by_policy"
    assert record_retention_action(_action(decision="outreach_only"))["status"] == "rejected"
    result = record_retention_action(_action())
    assert result == {"status": "recorded", "action_id": "ACC-1002--baseline-1", "action_status": "needs_approval"}
    doc = cosmos["ACC-1002--baseline-1"]
    assert doc["email_draft"]["to"] == "ana.souza@northwind.example"
    assert doc["offer"]["annual_value"] == 3456.0


def test_record_is_idempotent_per_usage_snapshot(cosmos, accounts):
    outreach = _action(account_id="ACC-1001", decision="outreach_only", offer_type="none", discount_percent=0)
    assert record_retention_action(outreach)["action_status"] == "draft_ready"
    assert record_retention_action(outreach)["status"] == "duplicate"
    accounts["ACC-1001"] = apply_usage_change(accounts["ACC-1001"], DROP["ACC-1001"], "usage-2", TODAY)
    assert record_retention_action(outreach)["action_id"] == "ACC-1001--usage-2"
    assert record_retention_action(_action(account_id="ACC-9999"))["status"] == "rejected"
