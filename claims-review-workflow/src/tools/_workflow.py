# Copyright (c) Microsoft Corporation.
# Licensed under the MIT License.

"""Shared plumbing for the claim workflow tools: load the claim and classify failures."""

from __future__ import annotations

from typing import Any

from azure.core.exceptions import ResourceNotFoundError, ServiceRequestError, ServiceResponseError

import _checks
import _claims
from azure_functions_agents import WorkflowRetryableError, WorkflowTerminalError


def claim_blob(args: dict[str, Any]) -> str:
    value = args.get("claim_blob")
    if not isinstance(value, str) or not value.strip():
        raise WorkflowTerminalError("invalid_arguments", "claim_blob must be a non-empty string.")
    try:
        return _claims.claim_path(value)
    except ValueError as exc:
        raise WorkflowTerminalError("invalid_arguments", str(exc)) from exc


def load_claim(blob: str) -> dict[str, Any]:
    """Re-read the claim from Blob Storage so amounts never pass through the model."""
    try:
        return _checks.normalize_claim(_claims.load_claim(blob))
    except ResourceNotFoundError as exc:
        raise WorkflowTerminalError("claim_not_found", f"Claim blob {blob} was not found.") from exc
    except (ServiceRequestError, ServiceResponseError) as exc:
        raise WorkflowRetryableError("storage_unavailable", "Blob Storage is temporarily unavailable.") from exc
    except ValueError as exc:
        raise WorkflowTerminalError("invalid_claim", str(exc)) from exc
