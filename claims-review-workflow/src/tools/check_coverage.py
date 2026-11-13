"""Workflow tool: check the claim against the policy's coverage."""

from typing import Any

import _checks
import _workflow
from azure_functions_agents import WorkflowRetryBackoff, WorkflowRetryPolicy, workflow_tool


@workflow_tool(
    description=(
        "Check policy coverage for one claim: policy found and active, incident inside the policy "
        "period, claim type covered, coverage limit and deductible, and estimated payout. "
        "Args: {claim_blob: <trigger blob name>}. Reads the claim from Blob Storage itself. "
        "Returns {check: 'coverage', claim_id, covered, estimated_payout, limit, deductible, issues}."
    ),
    retry=WorkflowRetryPolicy(
        max_attempts=3,
        backoff=WorkflowRetryBackoff(initial="PT2S", multiplier=2.0, max="PT10S"),
    ),
    timeout="PT1M",
)
def check_coverage(args: dict[str, Any]) -> dict[str, Any]:
    blob = _workflow.claim_blob(args)
    return _checks.check_coverage(_workflow.load_claim(blob))
