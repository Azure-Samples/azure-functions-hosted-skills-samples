# Copyright (c) Microsoft Corporation.
# Licensed under the MIT License.

from demo import validate


def test_validate_flags_missing_and_unapproved_reviews():
    results = {
        "e2e-abc123-INV-FAB-1001": {"decision": "approve_for_payment", "reasons": ["Clean match."]},
        "e2e-abc123-INV-NW-2002": {"decision": "hold_for_review", "reasons": ["Freight."], "requires_human_approval": False},
    }
    errors = validate(results, ["e2e-abc123-INV-FAB-1001", "e2e-abc123-INV-NW-2002", "e2e-abc123-INV-TS-3003"])
    assert errors == [
        "e2e-abc123-INV-NW-2002: hold_for_review without human approval",
        "e2e-abc123-INV-TS-3003: no review recorded",
    ]
