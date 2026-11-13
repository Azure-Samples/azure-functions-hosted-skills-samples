# Copyright (c) Microsoft Corporation.
# Licensed under the MIT License.

"""The contract review orchestration: validate, review with the hosted skill, route, record.

This is a plain generator so it can be unit-tested with a fake context; function_app.py
registers it as the Durable Functions orchestrator. Orchestrator code must be
deterministic: no I/O, clocks, or random values here, only ``context`` calls.
"""

from __future__ import annotations

from datetime import timedelta
from typing import Any, Generator

from durabletask.task import RetryPolicy

import contract_process as process

APPROVAL_EVENT = "LegalApproval"
AWAITING_APPROVAL = "awaiting_legal_approval"
REVIEW_RETRY = RetryPolicy(first_retry_interval=timedelta(seconds=15), max_number_of_attempts=3)


def review_process(context: Any) -> Generator[Any, Any, dict[str, Any]]:
    request = context.get_input() or {}
    raw_contract = request.get("contract")
    policy = request.get("policy") or process.policy_from_env()

    context.set_custom_status({"stage": "validating"})
    validation = yield context.call_activity("validate_contract", raw_contract)
    if not validation["valid"]:
        decision = process.build_decision(
            instance_id=context.instance_id,
            contract=raw_contract if isinstance(raw_contract, dict) else {},
            outcome="invalid",
            decided_at=context.current_utc_datetime.isoformat(),
            decided_by="validation",
            validation_errors=validation["errors"],
        )
        decision["record"] = yield context.call_activity("record_decision", decision)
        context.set_custom_status({"stage": "completed", "outcome": "invalid"})
        return decision

    contract = validation["contract"]
    contract_id = contract["contract_id"]
    context.set_custom_status({"stage": "reviewing", "contract_id": contract_id})
    review = yield context.call_activity_with_retry("review_contract", REVIEW_RETRY, contract)
    reasons = process.legal_review_reasons(contract, review, policy)

    approval = None
    if not reasons:
        outcome, decided_by = "auto_approved", "policy"
    else:
        due_at = context.current_utc_datetime + timedelta(hours=policy["approval_timeout_hours"])
        task = yield context.call_activity(
            "create_legal_review_task",
            {
                "instance_id": context.instance_id,
                "contract": contract,
                "review": review,
                "reasons": reasons,
                "created_at": context.current_utc_datetime.isoformat(),
                "due_at": due_at.isoformat(),
            },
        )
        context.set_custom_status(
            {
                "stage": AWAITING_APPROVAL,
                "contract_id": contract_id,
                "risk_level": review["risk_level"],
                "risk_score": review["risk_score"],
                "reasons": reasons,
                "task": task,
                "due_at": due_at.isoformat(),
            }
        )
        approval_event = context.wait_for_external_event(APPROVAL_EVENT)
        timeout = context.create_timer(due_at)
        winner = yield context.task_any([approval_event, timeout])
        if winner is approval_event:
            approval = process.normalize_approval(approval_event.result)
            outcome, decided_by = approval["decision"], approval["approver"]
        else:
            # Nobody decided in time: close the wait and hand the contract to the escalation path.
            outcome, decided_by = "escalated", "timeout"

    decision = process.build_decision(
        instance_id=context.instance_id,
        contract=contract,
        outcome=outcome,
        decided_at=context.current_utc_datetime.isoformat(),
        decided_by=decided_by,
        review=review,
        reasons=reasons,
        approval=approval,
    )
    context.set_custom_status({"stage": "recording", "contract_id": contract_id, "outcome": outcome})
    decision["record"] = yield context.call_activity("record_decision", decision)
    context.set_custom_status({"stage": "completed", "contract_id": contract_id, "outcome": outcome})
    return decision
