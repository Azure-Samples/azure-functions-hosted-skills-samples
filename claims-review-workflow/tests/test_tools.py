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


def test_publisher_ignores_tampered_history(store):
    args = {"claim_blob": "CLM-24003.json"}
    history = check_claim_history(args)
    history["fraud_signals"] = []
    store["inbox"]["CLM-24003.json"] = {**_sample("CLM-24003"), "amount_claimed": 900}
    result = publish_claim_decision({
        **args,
        "coverage": check_coverage(args),
        "documents": check_documents(args),
        "history": history,
        "recommended_decision": "approve",
    })
    record = store["saved"]["CLM-24003"]
    assert result["decision"] == "refer_to_adjuster"
    assert {s["code"] for s in record["fraud_signals"]} == {"frequent_claims", "new_policy"}


def test_publisher_rejects_results_from_another_claim(store):
    other = check_coverage({"claim_blob": "CLM-24002.json"})
    args = {"claim_blob": "CLM-24001.json"}
    with pytest.raises(WorkflowTerminalError) as exc:
        publish_claim_decision({
            **args, "coverage": other, "documents": check_documents(args), "history": check_claim_history(args),
        })
    assert exc.value.error_code == "check_claim_mismatch"
    with pytest.raises(WorkflowTerminalError) as exc:
        publish_claim_decision({**args, "coverage": "${check_coverage.result}"})
    assert exc.value.error_code == "invalid_check_result"
    assert store["saved"] == {}


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
