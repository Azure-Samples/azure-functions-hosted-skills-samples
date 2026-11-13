# Copyright (c) Microsoft Corporation.
# Licensed under the MIT License.

"""Records the investigation report for an alert group.

This tool only stores a report. Containment is a recommendation for an analyst: code
marks every recommended action as not executed and pending approval.

Do not add ``from __future__ import annotations`` here: the runtime loads tool
files dynamically and pydantic must resolve the model's types at import time.
"""

import re
from datetime import UTC, datetime
from typing import Any, Literal

import _storage
from pydantic import BaseModel, Field

_GROUP_ID = re.compile(r"^[A-Za-z0-9_.:-]{1,80}$")


class ContainmentRecommendation(BaseModel):
    action: Literal[
        "isolate_host",
        "disable_account",
        "revoke_sessions",
        "reset_credentials",
        "remove_role_assignment",
        "block_indicator",
        "quarantine_file",
        "preserve_evidence",
        "monitor",
    ]
    target: str = Field(description="Host, user, role assignment, IP, domain, or file hash the action applies to.")
    reason: str = Field(description="Why this action is needed, tied to the evidence.")
    urgency: Literal["immediate", "within_24h", "routine"]


class TimelineEntry(BaseModel):
    time: str = Field(description="ISO-8601 time from the tool output.")
    event: str


class InvestigationReport(BaseModel):
    group_id: str = Field(description="group_id from the queue message.")
    verdict: Literal["malicious", "suspicious", "benign", "inconclusive"]
    confidence: Literal["high", "medium", "low"]
    severity: Literal["critical", "high", "medium", "low", "informational"]
    summary: str = Field(description="Two or three sentences an on-call analyst can act on.")
    affected_entities: list[str] = Field(description="Hosts, users, and resources involved.")
    timeline: list[TimelineEntry] = Field(description="Key events in time order, from tool output.")
    evidence: list[str] = Field(description="Specific facts used, such as command lines, IPs, counts, and threat intel verdicts.")
    indicators: list[str] = Field(default_factory=list, description="IPs, domains, and file hashes involved.")
    recommended_containment: list[ContainmentRecommendation] = Field(
        default_factory=list, description="Actions for an analyst to approve. Never executed by this skill."
    )
    open_questions: list[str] = Field(default_factory=list, description="What a person still needs to check.")


def record_investigation(report: InvestigationReport) -> dict[str, Any]:
    """Record the investigation report for an alert group so an analyst can review it. Call once per group."""
    group_id = report.group_id.strip()
    if not _GROUP_ID.match(group_id):
        return {"status": "rejected", "error": "group_id must be 1-80 letters, digits, '.', ':', '-' or '_'."}

    document = report.model_dump()
    document["group_id"] = group_id
    # Code, not the model, decides that containment is only recommended and needs approval.
    document["recommended_containment"] = [
        {**action, "executed": False, "status": "pending_approval"} for action in document["recommended_containment"]
    ]
    document["requires_human_approval"] = bool(document["recommended_containment"]) or report.verdict in (
        "malicious",
        "suspicious",
    )
    document["recorded_at"] = datetime.now(UTC).isoformat()

    if not _storage.save_once(group_id, document):
        return {"status": "duplicate", "group_id": group_id}
    return {
        "status": "recorded",
        "group_id": group_id,
        "verdict": report.verdict,
        "requires_human_approval": document["requires_human_approval"],
        "containment_executed": False,
    }
