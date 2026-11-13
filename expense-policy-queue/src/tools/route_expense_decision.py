"""Routes one expense decision to its output queue.

The rules that must never depend on the model are enforced here, in code: the queue
must match the decision, a non-USD amount is never auto-approved, an approval needs a
positive amount, and each expenseId is routed at most once.

Do not add ``from __future__ import annotations`` here: the runtime loads tool
files dynamically and pydantic must resolve the model's types at import time.
"""

import json
import re
from datetime import UTC, datetime
from typing import Any, Literal

import _storage
from pydantic import BaseModel, Field

_EXPENSE_ID = re.compile(r"^[A-Za-z0-9_-]{1,64}$")
_CURRENCY = re.compile(r"^[A-Z]{3}$")


class ExpenseDecision(BaseModel):
    expenseId: str = Field(description="Expense ID from the message, or EXP- plus the first 8 characters of the queue message id.")
    decision: Literal["approve", "review", "flag"] = Field(description="Outcome of the policy rule that matched first.")
    queue: Literal["expense-approved", "expense-review", "expense-flagged"] = Field(
        description="approve -> expense-approved, review -> expense-review, flag -> expense-flagged."
    )
    amount: float | None = Field(default=None, description="Normalized amount as a number, or null if the message has none.")
    currency: str = Field(default="USD", description="ISO 4217 code, for example USD or EUR.")
    vendor: str = Field(default="", description="Vendor or merchant, if stated.")
    category: str = Field(description="Expense category, for example travel, meals, equipment, or general.")
    policyApplied: str = Field(description="Name of the policy document you read and applied, for example travel-policy.md.")
    reason: str = Field(description="One sentence naming the amount or situation and the policy rule that applied.")


def route_expense_decision(expense: ExpenseDecision) -> dict[str, Any]:
    """Send the decision for one expense request to its output queue. Call once per request."""
    expense_id = expense.expenseId.strip()
    currency = expense.currency.strip().upper()
    errors = []
    if not _EXPENSE_ID.match(expense_id):
        errors.append("expenseId must be 1-64 letters, digits, '-' or '_'.")
    if _storage.OUTPUT_QUEUES[expense.decision] != expense.queue:
        errors.append(f"decision '{expense.decision}' must go to queue '{_storage.OUTPUT_QUEUES[expense.decision]}', not '{expense.queue}'.")
    if not _CURRENCY.match(currency):
        errors.append("currency must be a 3-letter ISO 4217 code such as USD.")
    if expense.decision == "approve" and currency != "USD":
        errors.append("Non-USD amounts are never auto-approved; route them to review for FX verification.")
    if expense.decision == "approve" and (expense.amount is None or expense.amount <= 0):
        errors.append("An expense without a positive amount cannot be approved; flag it for clarification.")
    if not expense.reason.strip():
        errors.append("reason is required.")
    try:
        policy = _storage.policy_name(expense.policyApplied)
    except ValueError as exc:
        errors.append(str(exc))
        policy = expense.policyApplied
    if errors:
        return {"status": "rejected", "errors": errors, "note": "Fix the decision and call again."}

    message = {
        "expenseId": expense_id,
        "decision": expense.decision,
        "routedTo": expense.queue,
        "amount": expense.amount,
        "currency": currency,
        "vendor": expense.vendor.strip(),
        "category": expense.category.strip(),
        "policyApplied": policy,
        "reason": expense.reason.strip(),
        "decidedAt": datetime.now(UTC).isoformat(timespec="seconds"),
    }

    # Queue messages can be delivered more than once. The marker blob is written with
    # overwrite=False, so only the first run for an expenseId sends a decision.
    if not _storage.claim_decision(expense_id, message):
        return {"status": "duplicate", "expenseId": expense_id, "note": "Already routed; nothing was sent."}
    try:
        message_id = _storage.send_to_queue(expense.queue, json.dumps(message, separators=(",", ":")))
    except Exception as exc:
        _storage.release_decision(expense_id)
        return {"status": "error", "error": f"{type(exc).__name__}: {exc}"}
    return {"status": "routed", "expenseId": expense_id, "queue": expense.queue, "messageId": message_id}
