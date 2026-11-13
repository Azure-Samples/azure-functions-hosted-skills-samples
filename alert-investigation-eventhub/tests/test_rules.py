# Copyright (c) Microsoft Corporation.
# Licensed under the MIT License.

import json
from datetime import UTC, datetime

import pytest
from alert_router import rules

NOW = datetime(2026, 11, 12, 9, 30, tzinfo=UTC)


def alert(**overrides):
    base = {
        "alert_id": "A-1",
        "source": "edr",
        "rule_id": "rule-x",
        "title": "Test",
        "severity": "low",
        "host": "App-07",
        "user": "",
        "detected_at": "2026-11-12T09:00:00Z",
    }
    return {**base, **overrides}


def test_parse_normalizes_fields():
    parsed = rules.parse_alert(json.dumps(alert(severity="WEIRD", detected_at="nope")).encode(), NOW)
    assert parsed["rule_id"] == "RULE-X"
    assert parsed["host"] == "app-07"
    assert parsed["severity"] == "medium"
    assert parsed["detected_at"] == NOW.isoformat()
    assert parsed["evidence"] == {}


@pytest.mark.parametrize("body", [b"not json", b"[1,2]", json.dumps(alert(alert_id="bad id!")), json.dumps(alert(rule_id=""))])
def test_parse_rejects_invalid(body):
    with pytest.raises(rules.InvalidAlert):
        rules.parse_alert(body, NOW)


def test_fingerprint_ignores_alert_id_and_time():
    a = rules.parse_alert(alert(alert_id="A-1"))
    b = rules.parse_alert(alert(alert_id="A-2", detected_at="2026-11-12T09:10:00Z"))
    c = rules.parse_alert(alert(alert_id="A-3", host="app-08"))
    assert rules.fingerprint(a) == rules.fingerprint(b) != rules.fingerprint(c)
    escalated = rules.parse_alert(alert(alert_id="A-4", severity="critical"))
    assert rules.fingerprint(escalated) != rules.fingerprint(a)


def test_window_bucket():
    a = rules.parse_alert(alert(detected_at="2026-11-12T09:00:00Z"))
    b = rules.parse_alert(alert(detected_at="2026-11-12T09:59:00Z"))
    c = rules.parse_alert(alert(detected_at="2026-11-12T10:00:00Z"))
    assert rules.window_bucket(a, 60) == rules.window_bucket(b, 60) != rules.window_bucket(c, 60)


def test_suppression_matches_rule_host_and_tag():
    scan = rules.parse_alert(alert(rule_id="NET-PORT-SCAN", host="scan-01"))
    assert rules.match_suppression(scan)["id"] == "SUP-001"
    other_host = rules.parse_alert(alert(rule_id="NET-PORT-SCAN", host="fin-ws-22"))
    assert rules.match_suppression(other_host) is None
    tagged = rules.parse_alert(alert(rule_id="EDR-PUA-TOOL", host="scan-01"))
    assert rules.match_suppression(tagged)["id"] == "SUP-002"


def test_critical_is_never_suppressed():
    scan = rules.parse_alert(alert(rule_id="NET-PORT-SCAN", host="scan-01", severity="critical"))
    assert rules.match_suppression(scan) is None


def test_group_by_entity_and_primary():
    alerts = [
        rules.parse_alert(alert(alert_id="A-1", host="h1", severity="low")),
        rules.parse_alert(alert(alert_id="A-2", host="h1", severity="high")),
        rules.parse_alert(alert(alert_id="A-3", host="", user="u1")),
    ]
    groups = rules.group_alerts(alerts)
    assert [[a["alert_id"] for a in g] for g in groups] == [["A-2", "A-1"], ["A-3"]]
    assert rules.primary(groups[0])["alert_id"] == "A-2"
    assert rules.max_severity(groups[0]) == "high"
    assert not rules.is_critical(groups[0])


def test_build_state_includes_asset_context():
    state = rules.build_state([rules.parse_alert(alert(host="lt-apatel", user="a.patel"))])
    assert state["assets"]["user"]["privileged"] is True
    assert state["assets"]["host"]["criticality"] == "high"
    assert state["alerts"][0]["rule_id"] == "RULE-X"


def test_sample_burst_parses():
    from pathlib import Path

    samples = json.loads((Path(__file__).parent.parent / "samples" / "alert-burst.json").read_text(encoding="utf-8"))
    parsed = [rules.parse_alert(s, NOW) for s in samples]
    assert len({rules.fingerprint(a) for a in parsed if a["rule_id"] == "INFRA-DISK-SPACE"}) == 1
    assert sum(1 for a in parsed if rules.match_suppression(a)) == 1
