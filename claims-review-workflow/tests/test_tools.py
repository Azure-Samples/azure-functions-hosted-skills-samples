# Copyright (c) Microsoft Corporation.
# Licensed under the MIT License.

import json
from decimal import Decimal
from pathlib import Path

import _checks
import _claims
import pytest
import read_claim as read_claim_module
from azure_functions_agents import WorkflowTerminalError
from check_claim_history import check_claim_history
from check_coverage import check_coverage
from check_documents import check_documents
from publish_claim_decision import publish_claim_decision

TEMPLATE_DIR = Path(__file__).resolve().parent.parent
SAMPLES_DIR = TEMPLATE_DIR / "samples"
ATTACHMENTS_DIR = SAMPLES_DIR / "attachments"

EXPECTED = {
    "CLM-24001": "approve",
    "CLM-24002": "request_info",
    "CLM-24003": "refer_to_adjuster",
}


def _sample(claim_id: str) -> dict:
    return json.loads((SAMPLES_DIR / f"{claim_id}.json").read_text(encoding="utf-8"))


def _exists(file: str) -> bool:
    return (ATTACHMENTS_DIR / file).is_file()


@pytest.fixture
def store(monkeypatch):
    """Back the storage helpers with the committed samples and an in-memory decisions container."""
    inbox: dict[str, dict] = {}
    saved: dict[str, dict] = {}

    def load_claim(blob_name):
        path = _claims.claim_path(blob_name)
        if path in inbox:
            return inbox[path]
        return _sample(path.removesuffix(".json").split("-", 2)[-1] if path.startswith("e2e-") else path[:-5])

    def save_once(decision_id, document):
        if decision_id in saved:
            return False
        saved[decision_id] = json.loads(json.dumps(document))
        return True

    monkeypatch.setattr(_claims, "load_claim", load_claim)
    monkeypatch.setattr(_claims, "attachment_exists", _exists)
    monkeypatch.setattr(_claims, "read_attachment_text", lambda f: (ATTACHMENTS_DIR / f).read_text(encoding="utf-8"))
    monkeypatch.setattr(_claims, "save_decision_once", save_once)
    monkeypatch.setattr(_claims, "read_decision", saved.get)
    monkeypatch.delenv("CLAIMS_AUTO_APPROVE_LIMIT", raising=False)
    return {"inbox": inbox, "saved": saved}


def _run_workflow(blob: str, **model_args) -> dict:
    """Run the DAG the agent plans, wiring results the way the orchestrator resolves ${...}."""
    args = {"claim_blob": blob}
    return publish_claim_decision({
        **args,
        "coverage": check_coverage(args),
        "documents": check_documents(args),
        "history": check_claim_history(args),
        **model_args,
    })


def test_normalize_claim_rejects_bad_forms():
    with pytest.raises(_checks.ClaimError) as exc:
        _checks.normalize_claim({"claim_id": "X", "amount_claimed": -5, "incident_date": "yesterday"})
    message = str(exc.value)
    for fragment in ("policy_number", "claimant.name", "amount_claimed", "incident_date"):
        assert fragment in message


@pytest.mark.parametrize("claim_id", sorted(EXPECTED))
def test_samples_reach_expected_decisions(claim_id):
    claim = _checks.normalize_claim(_sample(claim_id))
    outcome = _checks.decide(
        claim,
        _checks.check_coverage(claim),
        _checks.check_documents(claim, _exists),
        _checks.check_claim_history(claim),
    )
    assert outcome["decision"] == EXPECTED[claim_id]
    assert outcome["reasons"]
    assert outcome["requires_human_review"] is (outcome["decision"] != "approve")


def test_coverage_payout_and_issues():
    claim = _checks.normalize_claim(_sample("CLM-24001"))
    result = _checks.check_coverage(claim)
    assert result["covered"] is True
    assert result["estimated_payout"] == 2700.00

    lapsed = _checks.normalize_claim({**_sample("CLM-24001"), "policy_number": "POL-A-0999"})
    issues = " ".join(_checks.check_coverage(lapsed)["issues"])
    assert "lapsed" in issues and "outside the policy period" in issues

    missing = _checks.normalize_claim({**_sample("CLM-24001"), "policy_number": "POL-NOPE"})
    assert _checks.check_coverage(missing)["policy_found"] is False


def test_documents_missing_and_not_uploaded():
    claim = _checks.normalize_claim(_sample("CLM-24002"))
    result = _checks.check_documents(claim, _exists)
    assert {m["type"]: m["reason"] for m in result["missing"]} == {
        "photos": "listed file CLM-24002/photos.txt was not uploaded",
        "plumber_report": "not listed on the claim form",
    }

    claim = _checks.normalize_claim(_sample("CLM-24001"))
    result = _checks.check_documents(claim, lambda f: "photos" not in f)
    assert result["missing"][0]["reason"].startswith("listed file")


def test_history_flags_fraud_signals():
    claim = _checks.normalize_claim(_sample("CLM-24003"))
    codes = {s["code"] for s in _checks.check_claim_history(claim)["fraud_signals"]}
    assert codes == {"frequent_claims", "new_policy"}

    late = _checks.normalize_claim({**_sample("CLM-24001"), "reported_date": "2026-12-15"})
    assert [s["code"] for s in _checks.check_claim_history(late)["fraud_signals"]] == ["late_reporting"]

    dup = _checks.normalize_claim({**_sample("CLM-24001"), "incident_date": "2024-08-14", "reported_date": "2024-08-14"})
    assert "possible_duplicate" in {s["code"] for s in _checks.check_claim_history(dup)["fraud_signals"]}


def test_fraud_or_amount_over_limit_is_never_approved():
    claim = _checks.normalize_claim(_sample("CLM-24001"))
    coverage = _checks.check_coverage(claim)
    documents = _checks.check_documents(claim, _exists)
    clean = _checks.check_claim_history(claim)
    fraud = {**clean, "fraud_signals": [{"code": "siu_watchlist", "detail": "x"}]}

    assert _checks.decide(claim, coverage, documents, clean)["decision"] == "approve"
    assert _checks.decide(claim, coverage, documents, fraud, recommended="approve")["decision"] == "refer_to_adjuster"
    over = _checks.decide(claim, coverage, documents, clean, recommended="approve", limit=Decimal("3000"))
    assert over["decision"] == "refer_to_adjuster" and over["auto_approved"] is False


def test_model_recommendation_only_makes_decision_stricter():
    claim = _checks.normalize_claim(_sample("CLM-24002"))
    coverage = _checks.check_coverage(claim)
    documents = _checks.check_documents(claim, _exists)
    history = _checks.check_claim_history(claim)
    assert _checks.decide(claim, coverage, documents, history, recommended="approve")["decision"] == "request_info"
    stricter = _checks.decide(claim, coverage, documents, history, recommended="refer_to_adjuster")
    assert stricter["decision"] == "refer_to_adjuster"
    assert stricter["reasons"][-1] == "Intake review recommended refer_to_adjuster."


def test_claim_path_normalizes_and_blocks_traversal():
    assert _claims.claim_path("claims-inbox/CLM-1.json") == "CLM-1.json"
    assert _claims.decision_id("claims-inbox/a/CLM-1.json") == "a__CLM-1"
    with pytest.raises(ValueError):
        _claims.claim_path("claims-inbox/../secrets.json")
    with pytest.raises(ValueError):
        _claims.safe_path("../x.txt", "claims-attachments")


@pytest.mark.parametrize("claim_id", sorted(EXPECTED))
def test_workflow_records_decision_once(store, claim_id):
    blob = f"claims-inbox/e2e-abc123-{claim_id}.json"
    result = _run_workflow(blob, recommended_decision="approve", intake_concerns=["none"], adjuster_note="Note.")
    assert result == {
        "status": "recorded",
        "decision_id": f"e2e-abc123-{claim_id}",
        "container": "claims-decisions",
        "decision": EXPECTED[claim_id],
    }
    record = store["saved"][f"e2e-abc123-{claim_id}"]
    assert record["claim_id"] == claim_id
    assert record["reasons"] and record["intake_concerns"] == ["none"]
    assert record["auto_approved"] is (EXPECTED[claim_id] == "approve")

    again = _run_workflow(blob)
    assert again["status"] == "duplicate" and again["decision"] == EXPECTED[claim_id]


def _all_clear(args: dict) -> dict:
    """Check results with the right labels that claim everything is fine."""
    coverage, documents, history = check_coverage(args), check_documents(args), check_claim_history(args)
    return {
        "coverage": {**coverage, "covered": True, "issues": [], "estimated_payout": coverage["amount_claimed"]},
        "documents": {**documents, "missing": [], "issues": [], "complete": True},
        "history": {**history, "fraud_signals": [], "clean": True},
    }


@pytest.mark.parametrize("claim_id", ["CLM-24002", "CLM-24003"])
def test_publisher_never_approves_on_fabricated_all_clear_results(store, claim_id):
    args = {"claim_blob": f"{claim_id}.json"}
    result = publish_claim_decision({**args, **_all_clear(args), "recommended_decision": "approve"})
    record = store["saved"][claim_id]
    assert result["decision"] == record["decision"] == "refer_to_adjuster"
    assert record["auto_approved"] is False and record["check_mismatches"]
    assert any(r.startswith("Check mismatch: ") for r in record["reasons"])
    # The record carries the recomputed results, not the fabricated ones.
    if claim_id == "CLM-24002":
        assert {m["type"] for m in record["documents"]["missing"]} == {"photos", "plumber_report"}
    else:
        assert {s["code"] for s in record["fraud_signals"]} == {"frequent_claims", "new_policy"}


def test_publisher_decides_on_recomputed_checks_only(store):
    # Fabricated history on a cheap CLM-24003 copy: still referred for the real fraud signals.
    store["inbox"]["CLM-24003.json"] = {**_sample("CLM-24003"), "amount_claimed": 900}
    args = {"claim_blob": "CLM-24003.json"}
    fabricated = _all_clear(args)
    result = publish_claim_decision({
        **args, "coverage": check_coverage(args), "documents": check_documents(args), "history": fabricated["history"],
    })
    record = store["saved"]["CLM-24003"]
    assert result["decision"] == "refer_to_adjuster"
    assert record["check_mismatches"] == ["history differs from the recomputed claim_history check in: fraud_signals."]
    assert {s["code"] for s in record["fraud_signals"]} == {"frequent_claims", "new_policy"}


def test_publisher_refers_missing_or_foreign_check_results(store):
    args = {"claim_blob": "CLM-24001.json"}
    other = check_coverage({"claim_blob": "CLM-24002.json"})
    result = publish_claim_decision({
        **args, "coverage": other, "documents": "${check_documents.result}", "recommended_decision": "approve",
    })
    record = store["saved"]["CLM-24001"]
    assert result["decision"] == "refer_to_adjuster"
    assert record["check_mismatches"] == [
        "coverage belongs to claim 'CLM-24002', not 'CLM-24001'.",
        "documents is not a documents check result.",
        "history is not a claim_history check result.",
    ]


def test_genuine_check_results_have_no_mismatches():
    claim = _checks.normalize_claim(_sample("CLM-24003"))
    recomputed = _checks.run_checks(claim, _exists)
    roundtrip = json.loads(json.dumps(recomputed))
    assert _checks.find_check_mismatches(roundtrip, recomputed) == []

def test_invalid_claim_is_terminal(store):
    store["inbox"]["bad.json"] = {"claim_id": "X"}
    with pytest.raises(WorkflowTerminalError) as exc:
        check_coverage({"claim_blob": "bad.json"})
    assert exc.value.error_code == "invalid_claim"


def test_read_claim_returns_form_and_excerpts(store, monkeypatch):
    result = read_claim_module.read_claim("claims-inbox/CLM-24002.json")
    assert result["valid"] is True and result["claim"]["claim_id"] == "CLM-24002"
    assert "7,800.00" in result["attachments"][0]["excerpt"]

    def broken(name):
        raise ValueError("Claim file is not valid JSON")

    monkeypatch.setattr(_claims, "load_claim", broken)
    assert read_claim_module.read_claim("x.json") == {
        "blob_name": "x.json", "valid": False, "errors": ["Claim file is not valid JSON"],
    }


def test_agent_plan_uses_existing_workflow_tools():
    agent = (TEMPLATE_DIR / "src" / "claims_review.agent.md").read_text(encoding="utf-8")
    assert "workflows:\n  enabled: true" in agent
    for tool in ("check_coverage", "check_documents", "check_claim_history", "publish_claim_decision"):
        assert f'"tool": "{tool}"' in agent
        assert (TEMPLATE_DIR / "src" / "tools" / f"{tool}.py").is_file()
