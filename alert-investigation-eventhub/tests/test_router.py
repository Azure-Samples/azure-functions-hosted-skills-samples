# Copyright (c) Microsoft Corporation.
# Licensed under the MIT License.

import json
from datetime import UTC, datetime

from alert_router import router
from alert_router.decision import ChoiceAnswer, DecisionError

NOW = datetime(2026, 11, 12, 9, 30, tzinfo=UTC)
SETTINGS = {"investigate_queue": "inv", "review_queue": "rev", "window_minutes": 60, "min_confidence": 0.5,
            "max_concurrency": 2}


class FakeStore:
    def __init__(self):
        self.fingerprints = {}
        self.decisions = {}
        self.queues = {"inv": [], "rev": []}
        self.events = []

    def claim_fingerprint(self, fingerprint, bucket, alert):
        key = (fingerprint, bucket)
        if key not in self.fingerprints:
            self.fingerprints[key] = {"first": alert["alert_id"], "count": 0}
            return "new"
        if self.fingerprints[key]["first"] == alert["alert_id"]:
            return "replay"
        self.fingerprints[key]["count"] += 1
        return "duplicate"

    def record_decision(self, record):
        self.events.append(("record", record["group_id"]))
        self.decisions[record["group_id"]] = record

    def enqueue(self, queue_name, message):
        self.events.append(("enqueue", message["group_id"]))
        self.queues[queue_name].append(json.loads(json.dumps(message)))


def body(alert_id, rule_id="R-1", severity="low", host="h1", user="", **extra):
    return json.dumps({"alert_id": alert_id, "source": "s", "rule_id": rule_id, "severity": severity, "host": host,
                       "user": user, "detected_at": "2026-11-12T09:00:00Z", **extra}).encode()


def decider_returning(choice, confidence=0.9):
    calls = []

    def decide(state):
        calls.append(state)
        rest = (1 - confidence) / 3
        return ChoiceAnswer(choice, {o: (confidence if o == choice else rest) for o in router.ROUTE_OPTIONS})

    decide.calls = calls
    return decide


def run(bodies, decider, store=None):
    store = store or FakeStore()
    records = router.route_batch(bodies, store=store, decider=decider, now=NOW, settings=SETTINGS)
    return store, {r["group_id"]: r for r in records}


def test_critical_bypasses_the_model():
    decider = decider_returning("routine")
    store, records = run([body("C-1", severity="critical")], decider)
    assert records["C-1"]["route"] == "investigate" and records["C-1"]["source"] == "critical_bypass"
    assert decider.calls == []
    assert store.queues["inv"][0]["group_id"] == "C-1"
    assert store.events == [("enqueue", "C-1"), ("record", "C-1")]


def test_critical_in_a_group_sends_the_whole_group():
    store, records = run([body("A-1", host="fin"), body("A-2", host="fin", severity="critical")], decider_returning("routine"))
    assert list(records) == ["A-2"]
    assert [a["alert_id"] for a in store.queues["inv"][0]["alerts"]] == ["A-2", "A-1"]


def test_decision_routes():
    for choice, queue in [("investigate", "inv"), ("aggregate", None), ("routine", None), ("uncertain", "rev")]:
        store, records = run([body("X-1")], decider_returning(choice))
        record = records["X-1"]
        assert record["source"] == "decision1"
        assert record["choice"] == choice
        assert record["probabilities"][choice] == 0.9
        if queue:
            assert len(store.queues[queue]) == 1
            assert record["destination"] == f"queue:{queue}"
        else:
            assert store.queues == {"inv": [], "rev": []}
            assert record["destination"] == "summary"
            assert record["route"] == choice


def test_low_confidence_goes_to_review():
    store, records = run([body("X-1")], decider_returning("routine", confidence=0.4))
    assert records["X-1"]["route"] == "review"
    assert records["X-1"]["source"] == "decision1_low_confidence"
    assert store.queues["rev"][0]["route"]["choice"] == "routine"


def test_decision_failure_fails_safe_to_review():
    def failing(_state):
        raise DecisionError("HTTP 500")

    def crashing(_state):
        raise KeyError("boom")

    for decider in (failing, crashing):
        store, records = run([body("X-1")], decider)
        assert records["X-1"]["route"] == "review" and records["X-1"]["source"] == "decision1_error"
        assert store.queues["rev"][0]["alerts"][0]["alert_id"] == "X-1"


def test_suppressed_alert_is_recorded_without_model_or_queue():
    decider = decider_returning("investigate")
    store, records = run([body("S-1", rule_id="NET-PORT-SCAN", host="scan-01")], decider)
    assert records["S-1"]["route"] == "suppressed" and "SUP-001" in records["S-1"]["reason"]
    assert decider.calls == [] and store.queues == {"inv": [], "rev": []}


def test_invalid_event_goes_to_review_with_raw_body():
    store, records = run([b"not json"], decider_returning("routine"))
    (record,) = records.values()
    assert record["route"] == "review" and record["source"] == "invalid_event"
    assert store.queues["rev"][0]["raw_event"] == "not json"


def test_duplicates_are_counted_and_dropped():
    decider = decider_returning("routine")
    store, records = run([body(f"D-{i}") for i in range(5)], decider)
    assert list(records) == ["D-0"] and records["D-0"]["alert_count"] == 1
    assert len(decider.calls) == 1
    (entry,) = store.fingerprints.values()
    assert entry == {"first": "D-0", "count": 4}


def test_duplicates_across_batches():
    store, _ = run([body("D-0")], decider_returning("routine"))
    _, records = run([body("D-1")], decider_returning("routine"), store=store)
    assert records == {}


def test_replay_of_the_same_alert_is_processed_again():
    store, _ = run([body("R-1")], decider_returning("investigate"))
    _, records = run([body("R-1")], decider_returning("investigate"), store=store)
    assert "R-1" in records
    assert len(store.queues["inv"]) == 2  # at-least-once; the skill writes the report idempotently


def test_related_alerts_are_grouped_into_one_decision():
    decider = decider_returning("investigate")
    _, records = run([body("G-1", rule_id="A", host="h9"), body("G-2", rule_id="B", host="h9", severity="high")], decider)
    assert list(records) == ["G-2"] and records["G-2"]["alert_ids"] == ["G-2", "G-1"]
    assert len(decider.calls) == 1 and decider.calls[0]["alert_count"] == 2


def test_summarize():
    _, records = run([body("A-1"), body("A-2"), body("C-1", host="h2", severity="critical")], decider_returning("routine"))
    assert router.summarize(list(records.values()), 3) == "received=3 groups=2 deduplicated=1 investigate=1 routine=1"
