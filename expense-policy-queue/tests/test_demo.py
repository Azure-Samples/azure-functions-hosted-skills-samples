# Copyright (c) Microsoft Corporation.
# Licensed under the MIT License.

from demo import EXPECTED, EXPECTED_STRICT, SAMPLE_TAGS, format_table, load_sample, validate, with_expense_id


def test_validate_checks_queue_policy_and_single_message():
    markers = {
        "EXP-TRV-1": {"routedTo": "expense-approved", "policyApplied": "travel-policy.md"},
        "EXP-DIN-1": {"routedTo": "expense-approved", "policyApplied": "meals-entertainment-policy.md", "reason": "x"},
        "EXP-EQP-1": {"routedTo": "expense-approved", "policyApplied": "general-expense-policy.md"},
    }
    queued = [
        {"queue": "expense-approved", "expenseId": "EXP-TRV-1"},
        {"queue": "expense-approved", "expenseId": "EXP-DIN-1"},
    ]
    expected = {
        "EXP-TRV-1": EXPECTED["travel"],
        "EXP-DIN-1": EXPECTED["client-dinner"],
        "EXP-EQP-1": EXPECTED["equipment"],
        "EXP-MIS-1": EXPECTED["missing-amount"],
    }
    assert validate(markers, queued, expected) == [
        "EXP-DIN-1: routed to 'expense-approved', expected 'expense-review' (x)",
        "EXP-EQP-1: applied 'general-expense-policy.md', expected 'equipment-software-policy.md'",
        "EXP-EQP-1: expected one message in 'expense-approved', found []",
        "EXP-MIS-1: no decision routed",
    ]


def test_strict_policy_changes_only_travel():
    assert EXPECTED["travel"][0] == "expense-approved"
    assert EXPECTED_STRICT["travel"][0] == "expense-review"
    assert EXPECTED_STRICT["equipment"] == EXPECTED["equipment"]


def test_samples_exist_and_get_an_expense_id():
    for name in SAMPLE_TAGS:
        assert load_sample(name)
    assert with_expense_id("EXP-1", "lunch $45") == "Expense ID: EXP-1\nlunch $45"


def test_format_table():
    assert format_table([]) == "No decisions in the output queues yet."
    table = format_table([{"queue": "expense-review", "expenseId": "EXP-1", "amount": 450.0, "reason": "Client dinner."}])
    assert table.splitlines()[1].startswith("expense-review | EXP-1 | 450.0")
