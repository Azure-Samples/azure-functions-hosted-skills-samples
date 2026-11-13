"""Workflow tool: apply the decision rule and record the claim decision (terminal step)."""

from datetime import UTC, datetime
from typing import Any

import _checks
import _claims
import _workflow
from azure_functions_agents import (
    WorkflowRetryBackoff,
    WorkflowRetryPolicy,
    WorkflowTerminalError,
    current_workflow_task_context,
    workflow_tool,
)


def _check_result(args: dict[str, Any], name: str, check: str, claim_id: str) -> dict[str, Any]:
    value = args.get(name)
    if not isinstance(value, dict) or value.get("check") != check:
        raise WorkflowTerminalError(
            "invalid_check_result", f"{name} must be the whole result of the {check} check task."
        )
    if value.get("claim_id") != claim_id:
        raise WorkflowTerminalError("check_claim_mismatch", f"{name} belongs to a different claim.")
    return value


def _text(value: Any, limit: int) -> str:
    return str(value).strip()[:limit] if isinstance(value, str) else ""


@workflow_tool(
    description=(
        "Final step. Decide the claim and write the decision record to the claims-decisions container. "
        "Args: {claim_blob: <trigger blob name>, coverage: '${check_coverage.result}', "
        "documents: '${check_documents.result}', history: '${check_claim_history.result}', "
        "recommended_decision: 'approve'|'request_info'|'refer_to_adjuster', "
        "intake_concerns: [short strings], adjuster_note: <one or two sentences>}. "
        "Code applies the rule: fraud signals, coverage problems, or an amount above the auto-approve "
        "limit always refer to an adjuster; missing documents request information. The recommendation "
        "can only make the outcome stricter. Writes once per claim blob; re-delivery returns 'duplicate'."
    ),
    retry=WorkflowRetryPolicy(
        max_attempts=3,
        backoff=WorkflowRetryBackoff(initial="PT2S", multiplier=2.0, max="PT10S"),
    ),
    timeout="PT1M",
)
def publish_claim_decision(args: dict[str, Any]) -> dict[str, Any]:
    blob = _workflow.claim_blob(args)
    claim = _workflow.load_claim(blob)
    coverage = _check_result(args, "coverage", "coverage", claim["claim_id"])
    documents = _check_result(args, "documents", "documents", claim["claim_id"])
    history = _check_result(args, "history", "claim_history", claim["claim_id"])

    # The hard rule does not depend on what the workflow passed in: re-read the amount
    # from the claim blob and recompute fraud signals from the source data.
    recomputed = _checks.check_claim_history(claim)
    if recomputed["fraud_signals"] != history.get("fraud_signals"):
        history = recomputed

    recommended = args.get("recommended_decision")
    if recommended not in _checks.DECISIONS:
        recommended = None
    outcome = _checks.decide(claim, coverage, documents, history, recommended=recommended)

    concerns = args.get("intake_concerns")
    concerns = [_text(c, 300) for c in concerns if isinstance(c, str)][:10] if isinstance(concerns, list) else []
    context = current_workflow_task_context()
    decision_id = _claims.decision_id(blob)
    record = {
        "decision_id": decision_id,
        "claim_blob": blob,
        "claim_id": claim["claim_id"],
        "policy_number": claim["policy_number"],
        "claim_type": claim["claim_type"],
        "claimant": claim["claimant"],
        "incident_date": claim["incident_date"].isoformat(),
        "amount_claimed": _checks.money(claim["amount_claimed"]),
        "estimated_payout": coverage.get("estimated_payout"),
        "auto_approve_limit": _checks.money(_checks.auto_approve_limit()),
        **outcome,
        "fraud_signals": history["fraud_signals"],
        "coverage": {
            k: coverage.get(k)
            for k in ("covered", "policy_status", "limit", "deductible", "estimated_payout", "issues")
        },
        "documents": {k: documents.get(k) for k in ("complete", "required", "provided", "missing")},
        "model_recommendation": args.get("recommended_decision"),
        "intake_concerns": concerns,
        "adjuster_note": _text(args.get("adjuster_note"), 1000),
        "workflow_id": context.workflow_id if context else None,
        "recorded_at": datetime.now(UTC).isoformat(timespec="seconds"),
    }
    created = _claims.save_decision_once(decision_id, record)
    return {
        "status": "recorded" if created else "duplicate",
        "decision_id": decision_id,
        "container": _claims.decisions_container_name(),
        "decision": record["decision"] if created else (_claims.read_decision(decision_id) or {}).get("decision"),
    }
