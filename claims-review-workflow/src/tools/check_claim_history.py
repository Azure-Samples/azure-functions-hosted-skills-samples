# Copyright (c) Microsoft Corporation.
# Licensed under the MIT License.

"""Workflow tool: look up prior claims and fraud signals for the policyholder."""

from typing import Any

import _checks
import _workflow
from azure_functions_agents import WorkflowRetryBackoff, WorkflowRetryPolicy, workflow_tool


@workflow_tool(
    description=(
        "Look up the policyholder's prior claims and flag fraud signals: SIU watchlist, frequent "
        "claims in the last 12 months, a brand-new policy, late reporting, and possible duplicates. "
        "Args: {claim_blob: <trigger blob name>}. "
        "Returns {check: 'claim_history', claim_id, prior_claims_12_months, fraud_signals: [{code, detail}], clean}."
    ),
    retry=WorkflowRetryPolicy(
        max_attempts=3,
        backoff=WorkflowRetryBackoff(initial="PT2S", multiplier=2.0, max="PT10S"),
    ),
    timeout="PT1M",
)
def check_claim_history(args: dict[str, Any]) -> dict[str, Any]:
    blob = _workflow.claim_blob(args)
    return _checks.check_claim_history(_workflow.load_claim(blob))
