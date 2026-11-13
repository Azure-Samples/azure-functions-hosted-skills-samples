from demo import DEMO_APPROVER, validate


def _done(output):
    return {"runtimeStatus": "COMPLETED", "output": {"record": "decisions/x.json", **output}}


def test_validate_accepts_expected_outcomes():
    results = {
        "CTR-2026-0142": _done({"outcome": "auto_approved", "risk_level": "low", "summary": "Clean."}),
        "CTR-2026-0187": _done({
            "outcome": "approved", "risk_level": "high", "summary": "Risky.",
            "legal_review_reasons": ["Risk score 100."], "deviations": [{"clause": "governing_law"}],
            "approval": {"approver": DEMO_APPROVER},
        }),
        "CTR-2026-0203": _done({"outcome": "invalid", "validation_errors": ["counterparty is required."]}),
    }
    assert validate(results) == []


def test_validate_flags_wrong_routing_and_missing_runs():
    results = {
        "CTR-2026-0142": _done({"outcome": "approved", "risk_level": "medium", "summary": "Hm."}),
        "CTR-2026-0203": {"runtimeStatus": "FAILED", "output": None},
    }
    assert validate(results) == [
        "CTR-2026-0142: outcome 'approved', expected 'auto_approved'",
        "CTR-2026-0187: not run",
        "CTR-2026-0203: runtime status FAILED",
    ]
