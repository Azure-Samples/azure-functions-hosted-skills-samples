# Copyright (c) Microsoft Corporation.
# Licensed under the MIT License.

from demo import validate


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
