# Copyright (c) Microsoft Corporation.
# Licensed under the MIT License.

import base64
import json
from datetime import UTC, datetime

import pytest
from alert_router import router
from alert_router.decision import ChoiceAnswer, DecisionError

NOW = datetime(2026, 11, 12, 9, 30, tzinfo=UTC)
QUEUE_LIMIT = 64 * 1024
SETTINGS = {"investigate_queue": "inv", "review_queue": "rev", "window_minutes": 60, "min_confidence": 0.5,
            "max_concurrency": 2}


class FakeStore:
    def __init__(self):
        self.fingerprints = {}
        self.decisions = {}
        self.queues = {"inv": [], "rev": []}
        self.sent = []
        self.blobs = {}
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

    def enqueue(self, queue_name, text):
        # Like Storage Queue: reject a message over 64 KiB, with plain or base64 encoding.
        if router.queue_message_size(text) > QUEUE_LIMIT or len(base64.b64encode(text.encode())) > QUEUE_LIMIT:
            raise ValueError("RequestBodyTooLarge")
        message = json.loads(text)
        self.events.append(("enqueue", message["group_id"]))
        self.sent.append(text)
        self.queues[queue_name].append(message)

    def save_payload(self, blob_name, text):
        self.blobs[blob_name] = text

    def full(self, message):
        """What the consumer sees: the message itself, or the payload it points to."""
        return json.loads(self.blobs[message["payload_blob"]]) if "payload_blob" in message else message


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


def huge_evidence(kb, marker):
    lines = [f"powershell -enc {'A' * 120} <&> line {i}" for i in range(kb * 1024 // 140)]
    return {"command_lines": lines, "marker": marker}


def test_small_message_is_queued_inline():
    store, records = run([body("C-1", severity="critical")], decider_returning("routine"))
    assert "alerts" in store.queues["inv"][0] and store.blobs == {}
    assert records["C-1"]["payload_blob"] == ""


def test_single_huge_event_is_offloaded_to_blob():
    evidence = huge_evidence(300, "marker-end")
    store, records = run([body("C-1", severity="critical", evidence=evidence)], decider_returning("routine"))
    (pointer,) = store.queues["inv"]
    assert pointer["payload_blob"] == "inv/C-1.json" and "alerts" not in pointer
    assert records["C-1"]["payload_blob"] == "inv/C-1.json"
    assert pointer["route"]["source"] == "critical_bypass" and pointer["alert_count"] == 1
    assert store.full(pointer)["alerts"][0]["evidence"] == evidence  # nothing lost
    assert all(router.queue_message_size(t) <= router.MAX_QUEUE_MESSAGE_BYTES for t in store.sent)


def test_batch_of_large_events_never_exceeds_the_queue_limit():
    bodies = []
    for i in range(40):
        bodies.append(body(f"L-{i}", rule_id=f"R-{i}", host=f"h{i % 4}", severity="critical" if i % 2 else "medium",
                           evidence=huge_evidence(20, f"m{i}"), description="&<>" * 1000, title="T" * 5000))
    bodies.append(b"{" + b"x" * 200_000)  # invalid and huge
    decider = decider_returning("uncertain")
    store, records = run(bodies, decider)
    assert all(router.queue_message_size(t) <= router.MAX_QUEUE_MESSAGE_BYTES for t in store.sent)
    assert all(len(base64.b64encode(t.encode())) <= QUEUE_LIMIT for t in store.sent)
    # Every alert reached a queue in full, including the invalid event's raw body.
    delivered = {a["alert_id"]: a for q in store.queues.values() for m in q for a in store.full(m).get("alerts", [])}
    assert set(delivered) == {f"L-{i}" for i in range(40)}
    assert delivered["L-7"]["evidence"]["marker"] == "m7" and len(delivered["L-7"]["title"]) == 500
    (invalid,) = [store.full(m) for m in store.queues["rev"] if m["group_id"].startswith("invalid-")]
    assert len(invalid["raw_event"]) == 200_001
    assert all(r["payload_blob"] for r in records.values())


def test_offload_is_idempotent_on_retry():
    evidence = huge_evidence(100, "x")
    store, _ = run([body("C-1", severity="critical", evidence=evidence)], decider_returning("routine"))
    run([body("C-1", severity="critical", evidence=evidence)], decider_returning("routine"), store=store)
    assert list(store.blobs) == ["inv/C-1.json"] and len(store.queues["inv"]) == 2


def test_blob_failure_raises_before_anything_is_queued_or_recorded():
    class FailingBlobStore(FakeStore):
        def save_payload(self, blob_name, text):
            raise OSError("blob unavailable")

    store = FailingBlobStore()
    with pytest.raises(OSError):
        run([body("C-1", severity="critical", evidence=huge_evidence(100, "x"))], decider_returning("routine"), store)
    assert store.queues == {"inv": [], "rev": []} and store.decisions == {}
