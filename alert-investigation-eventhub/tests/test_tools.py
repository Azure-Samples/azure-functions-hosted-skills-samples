# Copyright (c) Microsoft Corporation.
# Licensed under the MIT License.

import json

import _storage
import pytest
from get_alert_payload import get_alert_payload
from execute_containment_action import execute_containment_action
from get_asset import get_asset
from get_endpoint_events import get_endpoint_events
from get_network_flows import get_network_flows
from get_signin_logs import get_signin_logs
from lookup_threat_intel import lookup_threat_intel
from record_investigation import InvestigationReport, record_investigation

AS_OF = "2026-11-12T09:30:00+00:00"


@pytest.fixture
def saved(monkeypatch):
    store = {}

    def save_once(group_id, document):
        if group_id in store:
            return False
        store[group_id] = document
        return True

    monkeypatch.setattr(_storage, "save_once", save_once)
    return store


def report(**overrides):
    base = {
        "group_id": "ALR-1001-abc123",
        "verdict": "malicious",
        "confidence": "high",
        "severity": "critical",
        "summary": "Ransomware on fin-ws-22.",
        "affected_entities": ["fin-ws-22", "m.garcia"],
        "timeline": [{"time": AS_OF, "event": "vssadmin delete shadows"}],
        "evidence": ["vssadmin.exe delete shadows /all /quiet"],
        "indicators": ["185.220.101.47"],
        "recommended_containment": [
            {"action": "isolate_host", "target": "fin-ws-22", "reason": "Active encryption", "urgency": "immediate"}
        ],
    }
    return InvestigationReport(**{**base, **overrides})


def test_get_asset_host_user_and_unknown():
    assert get_asset("FIN-WS-22")["criticality"] == "high"
    assert get_asset("a.patel")["privileged"] is True
    assert "error" in get_asset("nope")


def test_logs_are_anchored_to_as_of():
    result = get_endpoint_events("fin-ws-22", AS_OF)
    assert result["count"] == 7
    assert result["events"][0]["time"] == "2026-11-12T09:11:00+00:00"
    assert result["events"][-1]["process"] == "rundll32.exe"
    assert "offset_minutes" not in result["events"][0]


def test_lookback_filters_old_events():
    assert get_signin_logs("a.patel", AS_OF, lookback_hours=1)["count"] == 8
    assert get_signin_logs("a.patel", AS_OF, lookback_hours=24)["count"] == 9
    assert get_network_flows("unknown-host", AS_OF)["count"] == 0


def test_threat_intel():
    assert lookup_threat_intel("185.220.101.47")["verdict"] == "malicious"
    assert lookup_threat_intel("8.8.8.8")["verdict"] == "unknown"


def test_containment_is_always_refused():
    result = execute_containment_action("isolate_host", "fin-ws-22")
    assert result["status"] == "refused" and result["executed"] is False


def test_record_marks_containment_not_executed(saved):
    result = record_investigation(report())
    assert result["status"] == "recorded" and result["containment_executed"] is False
    doc = saved["ALR-1001-abc123"]
    assert doc["requires_human_approval"] is True
    assert doc["recommended_containment"][0] | {} == {
        "action": "isolate_host", "target": "fin-ws-22", "reason": "Active encryption", "urgency": "immediate",
        "executed": False, "status": "pending_approval",
    }


def test_record_requires_approval_for_suspicious_even_without_containment(saved):
    record_investigation(report(verdict="suspicious", recommended_containment=[]))
    assert saved["ALR-1001-abc123"]["requires_human_approval"] is True


def test_benign_without_containment_needs_no_approval(saved):
    record_investigation(report(verdict="benign", recommended_containment=[]))
    assert saved["ALR-1001-abc123"]["requires_human_approval"] is False


def test_record_is_idempotent(saved):
    assert record_investigation(report())["status"] == "recorded"
    assert record_investigation(report())["status"] == "duplicate"


def test_record_rejects_bad_group_id(saved):
    assert record_investigation(report(group_id="../etc/passwd"))["status"] == "rejected"
    assert saved == {}


def test_get_alert_payload_reads_the_offloaded_group(monkeypatch):
    payload = {"group_id": "C-1", "alerts": [{"alert_id": "C-1", "evidence": {"cmd": "x" * 10_000, "ips": list(range(80))}}]}
    reads = []

    def load_payload(blob_name):
        reads.append(blob_name)
        return json.dumps(payload) if blob_name == "alerts-investigate/C-1.json" else None

    monkeypatch.setattr(_storage, "load_payload", load_payload)
    monkeypatch.delenv("INVESTIGATE_QUEUE", raising=False)
    result = get_alert_payload("C-1")
    evidence = result["alerts"][0]["evidence"]
    assert result["payload_blob"] == "alerts-investigate/C-1.json"
    assert evidence["cmd"].endswith("[truncated 6000 more characters]") and len(evidence["ips"]) == 51
    assert "error" in get_alert_payload("C-2")
    assert "error" in get_alert_payload("../alerts-review/C-1")
    assert reads == ["alerts-investigate/C-1.json", "alerts-investigate/C-2.json"]
