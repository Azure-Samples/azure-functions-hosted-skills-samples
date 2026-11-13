# Copyright (c) Microsoft Corporation.
# Licensed under the MIT License.

from check_contract_terms import ContractTerms, check_contract_terms
from get_clause_playbook import get_clause_playbook
from get_standard_clause import get_standard_clause

STANDARD_SAAS = dict(
    contract_type="saas_subscription",
    liability_cap_months=12,
    liability_cap_covers_data_breach=False,
    customer_liability_uncapped=False,
    payment_days=45,
    termination_for_convenience=True,
    termination_notice_days=30,
    auto_renews=True,
    renewal_notice_days=30,
    renewal_price_increase_pct=5,
    indemnity="mutual",
    governing_law="Washington",
    processes_personal_data=True,
    dpa_included=True,
    breach_notification_hours=72,
)


def _statuses(result):
    return {f["clause"]: f["status"] for f in result["findings"]}


def test_standard_contract_scores_zero():
    result = check_contract_terms(ContractTerms(**STANDARD_SAAS))
    assert result["risk_score"] == 0
    assert result["risk_level"] == "low"
    assert result["non_standard_clauses"] == []
    assert set(_statuses(result).values()) == {"standard"}


def test_acceptable_fallbacks_stay_low_risk():
    terms = ContractTerms(**{**STANDARD_SAAS, "payment_days": 30, "governing_law": "State of Delaware", "renewal_notice_days": 60})
    result = check_contract_terms(terms)
    statuses = _statuses(result)
    assert statuses["payment_terms"] == "acceptable"
    assert statuses["governing_law"] == "acceptable"
    assert statuses["auto_renewal"] == "acceptable"
    assert result["risk_score"] == 15
    assert result["risk_level"] == "low"
    assert result["non_standard_clauses"] == []


def test_high_risk_master_services_agreement():
    terms = ContractTerms(
        contract_type="master_services",
        liability_cap_months=3,
        liability_cap_covers_data_breach=True,
        customer_liability_uncapped=True,
        payment_days=15,
        termination_for_convenience=False,
        indemnity="customer_only",
        governing_law="Texas",
        processes_personal_data=True,
        dpa_included=False,
    )
    result = check_contract_terms(terms)
    assert result["risk_level"] == "high"
    assert result["risk_score"] == 100
    assert set(result["non_standard_clauses"]) == {
        "limitation_of_liability", "payment_terms", "termination", "indemnification", "governing_law", "data_protection",
    }


def test_missing_clauses_are_flagged():
    result = check_contract_terms(ContractTerms(contract_type="master_services", payment_days=45))
    statuses = _statuses(result)
    assert statuses["payment_terms"] == "standard"
    assert statuses["indemnification"] == "missing"
    assert "governing_law" in result["non_standard_clauses"]


def test_uncapped_renewal_increase_is_non_standard():
    result = check_contract_terms(ContractTerms(**{**STANDARD_SAAS, "renewal_price_increase_pct": None}))
    assert _statuses(result)["auto_renewal"] == "non_standard"


def test_unknown_contract_type():
    assert "error" in check_contract_terms(ContractTerms(contract_type="lease"))
    assert "error" in get_clause_playbook("lease")


def test_playbook_lists_required_clauses():
    playbook = get_clause_playbook("SaaS_Subscription")
    assert "auto_renewal" in playbook["clauses"]
    assert playbook["clauses"]["limitation_of_liability"]["severity"] == "high"
    assert "auto_renewal" not in get_clause_playbook("master_services")["clauses"]


def test_standard_clause_language():
    assert "twelve (12) months" in get_standard_clause("limitation_of_liability")["standard_language"]
    assert "error" in get_standard_clause("force_majeure")
