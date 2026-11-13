# Copyright (c) Microsoft Corporation.
# Licensed under the MIT License.

from demo import action_id, validate


def _doc(**overrides):
    doc = {
        "decision": "retention_offer",
        "status": "needs_approval",
        "tier": "business",
        "offer": {"type": "discount", "discount_percent": 12, "annual_value": 3456.0},
        "email_draft": {"to": "ana.souza@northwind.example", "subject": "Renewal", "body": "Hi Ana"},
    }
    doc.update(overrides)
    return doc


def test_action_id_matches_tool_rule():
    assert action_id("ACC-1002", "usage-20261112T231000Z-ab12") == "ACC-1002--usage-20261112T231000Z-ab12"
    assert action_id("ACC-1002", "a/b#c") == "ACC-1002--a_b_c"


def test_validate_flags_missing_over_limit_and_unapproved_actions():
    expected = {"ACC-1001": "ACC-1001--s", "ACC-1002": "ACC-1002--s"}
    results = {"ACC-1002--s": _doc(status="draft_ready", offer={"type": "discount", "discount_percent": 18, "annual_value": 5184.0})}
    assert validate(results, expected) == [
        "ACC-1001: no retention action recorded",
        "ACC-1002: discount 18% is above the business limit",
        "ACC-1002: offer above the approval threshold is not marked needs_approval",
    ]
    assert validate({"ACC-1002--s": _doc()}, {"ACC-1002": "ACC-1002--s"}) == []
