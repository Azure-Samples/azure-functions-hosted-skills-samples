# Copyright (c) Microsoft Corporation.
# Licensed under the MIT License.

import json
from datetime import UTC, datetime

import demo

NOW = datetime(2026, 11, 12, 9, 30, 15, 123, tzinfo=UTC)


def rec(group_id, route, source, alert_ids, probabilities=None):
    return {"group_id": group_id, "route": route, "source": source, "alert_ids": alert_ids,
            "alert_count": len(alert_ids), "probabilities": probabilities or {}}


def good_state():
    probs = {"routine": 0.9, "investigate": 0.02, "aggregate": 0.05, "uncertain": 0.03}
    records = {
        "ALR-1001": [rec("ALR-1001-r", "investigate", "critical_bypass", ["ALR-1001-r"])],
        "ALR-1002": [rec("ALR-1002-r", "investigate", "decision1", ["ALR-1002-r"], probs)],
        "ALR-1003": [rec("ALR-1003-r", "routine", "decision1", ["ALR-1003-r"], probs)],
        "ALR-1008": [rec("ALR-1008-r", "routine", "decision1", ["ALR-1008-r"], probs)],
        "ALR-1009": [rec("ALR-1009-r", "suppressed", "suppression_rule", ["ALR-1009-r"])],
        "ALR-1010": [rec("ALR-1010-r", "aggregate", "decision1", ["ALR-1010-r"], probs)],
        "ALR-1011": [{**rec("ALR-1011-r", "investigate", "critical_bypass", ["ALR-1011-r"]),
                      "payload_blob": "alerts-investigate/ALR-1011-r.json"}],
    }
    report = {"verdict": "malicious", "evidence": ["x"], "requires_human_approval": True,
              "recommended_containment": [{"action": "isolate_host", "target": "fin-ws-22", "executed": False}]}
    return {
        "records": {**{d: [] for d in demo.DUPLICATES}, **records},
        "duplicate_count": 4,
        "reports": {"ALR-1001-r": report, "ALR-1002-r": dict(report),
                    "ALR-1011-r": {**report, "evidence": ["rclone.exe uploaded 1,800 files"]}},
        "investigate_groups": ["ALR-1001-r", "ALR-1002-r", "ALR-1011-r"],
    }


def test_prepare_run_makes_ids_and_fingerprints_unique():
    samples = demo.load_samples()
    alerts = demo.prepare_run(samples, "abc123", NOW)
    assert len(alerts) == 10
    assert all(a["alert_id"].endswith("-abc123") and a["source"].endswith("#abc123") for a in alerts)
    assert {a["detected_at"] for a in alerts} == {"2026-11-12T09:30:15+00:00"}


def test_validate_passes_for_expected_outcome():
    state = good_state()
    assert demo.is_complete(state)
    assert demo.validate(state) == ([], [])


def test_validate_notes_but_does_not_fail_judgment_differences():
    state = good_state()
    state["records"]["ALR-1010"][0]["route"] = "routine"
    errors, notes = demo.validate(state)
    assert errors == [] and len(notes) == 1


def test_validate_fails_on_rule_violations():
    state = good_state()
    state["records"]["ALR-1001"][0]["source"] = "decision1"
    state["records"]["ALR-1004"] = [rec("ALR-1004-r", "routine", "decision1", ["ALR-1004-r"])]
    state["records"]["ALR-1009"][0]["route"] = "routine"
    state["reports"]["ALR-1002-r"] = None
    del state["reports"]["ALR-1002-r"]
    errors, _ = demo.validate(state)
    assert any("ALR-1001" in e for e in errors)
    assert any("ALR-1004" in e for e in errors)
    assert any("ALR-1009" in e for e in errors)
    assert any("ALR-1002-r" in e for e in errors)


def test_validate_fails_when_containment_was_executed():
    state = good_state()
    state["reports"]["ALR-1001-r"] = {**state["reports"]["ALR-1001-r"],
                                      "recommended_containment": [{"action": "isolate_host", "executed": True}]}
    errors, _ = demo.validate(state)
    assert any("executed=false" in e for e in errors)


def test_incomplete_until_reports_exist():
    state = good_state()
    del state["reports"]["ALR-1002-r"]
    assert not demo.is_complete(state)


def test_oversized_alert_is_larger_than_a_queue_message():
    alert = demo.oversized_alert("abc123", NOW)
    assert len(json.dumps(alert)) > 2 * 64 * 1024
    assert alert["alert_id"] == "ALR-1011-abc123" and alert["severity"] == "critical"


def test_validate_fails_when_oversized_alert_was_not_offloaded_or_read():
    state = good_state()
    state["records"]["ALR-1011"][0]["payload_blob"] = ""
    assert any("ALR-1011" in e for e in demo.validate(state)[0])

    state = good_state()
    state["reports"]["ALR-1011-r"] = {**state["reports"]["ALR-1011-r"], "evidence": ["nothing specific"]}
    assert any("ALR-1011" in e for e in demo.validate(state)[0])
