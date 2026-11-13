# Copyright (c) Microsoft Corporation.
# Licensed under the MIT License.

"""Workflow tool: check that the documents required for the claim type were submitted."""

from typing import Any

import _checks
import _claims
import _workflow
from azure_functions_agents import WorkflowRetryBackoff, WorkflowRetryPolicy, workflow_tool


@workflow_tool(
    description=(
        "Check that every document required for the claim type is listed on the claim form and was "
        "uploaded to the claims-attachments container. Args: {claim_blob: <trigger blob name>}. "
        "Returns {check: 'documents', claim_id, required, provided, missing: [{type, description, reason}], complete}."
    ),
    retry=WorkflowRetryPolicy(
        max_attempts=3,
        backoff=WorkflowRetryBackoff(initial="PT2S", multiplier=2.0, max="PT10S"),
    ),
    timeout="PT1M",
)
def check_documents(args: dict[str, Any]) -> dict[str, Any]:
    blob = _workflow.claim_blob(args)
    return _checks.check_documents(_workflow.load_claim(blob), _claims.attachment_exists)
