# Copyright (c) Microsoft Corporation.
# Licensed under the MIT License.

import json

from demo import malformed_bodies, validate, validate_malformed


def test_validate_flags_missing_and_unapproved_recommendations():
    results = {
        "RR-1002-abc": {"decision": "approve_standard", "reasons": ["Within 30 days."], "requires_human_approval": False},
        "RR-1003-abc": {"decision": "decline", "reasons": ["Used item."], "requires_human_approval": False},
    }
    errors = validate(results, ["RR-1001-abc", "RR-1002-abc", "RR-1003-abc"])
    assert errors == [
        "RR-1001-abc: no recommendation recorded",
        "RR-1003-abc: non-standard decision without human approval",
    ]


def test_malformed_bodies_are_distinct_and_unusable():
    bodies = malformed_bodies("abc123")
    assert len(set(bodies)) == 2
    for body in bodies:
        try:
            request = json.loads(body)
        except json.JSONDecodeError:
            continue
        assert "request_id" not in request or "order_id" not in request


def test_validate_malformed_requires_one_escalation_per_message():
    results = {
        "msg-1": {"decision": "escalate", "requires_human_approval": True},
        "msg-2": {"decision": "decline", "requires_human_approval": True},
    }
    assert validate_malformed(results, ["msg-1", "msg-2", "msg-3"]) == [
        "msg-2: malformed message recorded as 'decline', expected escalate",
        "msg-3: malformed message was not escalated",
    ]
