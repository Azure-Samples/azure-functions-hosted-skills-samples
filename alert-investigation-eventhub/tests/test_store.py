# Copyright (c) Microsoft Corporation.
# Licensed under the MIT License.

import json

import pytest
from alert_router.store import AzureAlertStore
from azure.core.exceptions import HttpResponseError, ResourceExistsError


class Entity(dict):
    def __init__(self, data, etag):
        super().__init__(data)
        self.metadata = {"etag": etag}


class FakeTable:
    def __init__(self, conflicts=0):
        self.rows = {}
        self.etag = 0
        self.conflicts = conflicts

    def create_entity(self, entity):
        key = (entity["PartitionKey"], entity["RowKey"])
        if key in self.rows:
            raise ResourceExistsError("exists")
        self.rows[key] = dict(entity)

    def get_entity(self, pk, rk):
        return Entity(self.rows[(pk, rk)], str(self.etag))

    def update_entity(self, entity, etag, match_condition):
        if self.conflicts:
            self.conflicts -= 1
            self.etag += 1
            error = HttpResponseError("precondition failed")
            error.status_code = 412
            raise error
        assert etag == str(self.etag)
        self.rows[(entity["PartitionKey"], entity["RowKey"])] = dict(entity)
        self.etag += 1

    def upsert_entity(self, entity):
        self.rows[(entity["PartitionKey"], entity["RowKey"])] = dict(entity)


@pytest.fixture
def store():
    s = AzureAlertStore()
    tables = {}
    s._table = lambda name: tables.setdefault(name, FakeTable())
    s.tables = tables
    return s


def alert(alert_id):
    return {"alert_id": alert_id, "rule_id": "R", "host": "h", "user": ""}


def test_claim_new_replay_and_duplicate(store):
    assert store.claim_fingerprint("fp", 7, alert("A-1")) == "new"
    assert store.claim_fingerprint("fp", 7, alert("A-1")) == "replay"
    assert store.claim_fingerprint("fp", 7, alert("A-2")) == "duplicate"
    assert store.claim_fingerprint("fp", 7, alert("A-3")) == "duplicate"
    row = store.tables["alertfingerprints"].rows[("fp", "7")]
    assert row["FirstAlertId"] == "A-1" and row["DuplicateCount"] == 2 and row["LastDuplicateAlertId"] == "A-3"
    assert store.claim_fingerprint("fp", 8, alert("A-4")) == "new"


def test_claim_retries_on_concurrent_update(store):
    store.claim_fingerprint("fp", 1, alert("A-1"))
    store.tables["alertfingerprints"].conflicts = 2
    assert store.claim_fingerprint("fp", 1, alert("A-2")) == "duplicate"
    assert store.tables["alertfingerprints"].rows[("fp", "1")]["DuplicateCount"] == 1


def test_record_decision_serializes_lists(store):
    store.record_decision({
        "group_id": "G-1", "routed_at": "2026-11-12T09:30:00+00:00", "route": "routine", "confidence": 0.9,
        "alert_ids": ["G-1", "G-2"], "probabilities": {"routine": 0.9},
    })
    row = store.tables["alertdecisions"].rows[("2026-11-12", "G-1")]
    assert row["route"] == "routine" and row["confidence"] == 0.9
    assert json.loads(row["alert_ids"]) == ["G-1", "G-2"]
    assert json.loads(row["probabilities"]) == {"routine": 0.9}
