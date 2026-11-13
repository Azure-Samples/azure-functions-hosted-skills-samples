# Copyright (c) Microsoft Corporation.
# Licensed under the MIT License.

from demo import _sample_id, validate


def test_validate_flags_missing_unsafe_and_unreviewed_decisions():
    ok = {"reasons": ["r"], "workflow_id": "wf", "requires_human_review": True}
    results = {
        "e2e-abc123-CLM-24001": {"decision": "approve", "reasons": ["Clean."], "workflow_id": "wf", "amount_claimed": 3200},
        "e2e-abc123-CLM-24002": {**ok, "decision": "request_info", "requires_human_review": False},
        "e2e-abc123-CLM-24003": {**ok, "decision": "approve", "amount_claimed": 48500, "fraud_signals": [{"code": "x"}]},
        "e2e-abc123-CLM-24004": {"decision": "refer_to_adjuster", "reasons": ["r"], "requires_human_review": True},
    }
    errors = validate(results, [*results, "e2e-abc123-CLM-24005"])
    assert errors == [
        "e2e-abc123-CLM-24002: request_info without human review",
        "e2e-abc123-CLM-24003: approved with fraud signals",
        "e2e-abc123-CLM-24003: approved above the auto-approve limit 10000.0",
        "e2e-abc123-CLM-24004: no workflow_id; the decision was not written by the workflow",
        "e2e-abc123-CLM-24005: no decision recorded",
    ]


def test_sample_id_strips_e2e_prefix():
    assert _sample_id("e2e-abc123-CLM-24001") == "CLM-24001"
    assert _sample_id("CLM-24001") == "CLM-24001"
