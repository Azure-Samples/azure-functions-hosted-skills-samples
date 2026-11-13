"""Records the hosted skill's retention action for the customer success team.

This tool only stores a draft. Nothing is emailed and no price changes: a person reviews
every action, and offers above the policy threshold are marked ``needs_approval``.

Do not add ``from __future__ import annotations`` here: the runtime loads tool
files dynamically and pydantic must resolve the model's types at import time.
"""

import re
from datetime import UTC, datetime
from typing import Any, Literal

import _cosmos
from _policy import evaluate_offer
from _sample_data import normalize_account_id
from pydantic import BaseModel, Field

_UNSAFE_ID_CHARS = re.compile(r"[^A-Za-z0-9._:-]")


class RetentionAction(BaseModel):
    account_id: str = Field(description="Account ID from the trigger data, for example ACC-1001.")
    decision: Literal["retention_offer", "outreach_only"] = Field(
        description="retention_offer when you propose an offer; outreach_only for a check-in without an offer."
    )
    risk_level: Literal["low", "medium", "high"] = Field(description="risk_level from assess_churn_signals.")
    churn_signals: list[str] = Field(description="Signals from assess_churn_signals and anything you found in tickets or history.")
    likely_cause: str = Field(description="One or two sentences on why usage is dropping, based on the evidence.")
    offer_type: Literal["none", "discount", "success_package", "seat_rightsizing"] = Field(
        description="none for outreach_only. Must be an offer allowed by get_plan_catalog."
    )
    discount_percent: float = Field(default=0, description="Renewal discount in percent; only for offer_type discount.")
    offer_summary: str = Field(default="", description="One sentence describing the offer; empty for outreach_only.")
    email_subject: str = Field(description="Subject of the outreach email draft.")
    email_body: str = Field(description="Body of the outreach email draft, signed by the account's CSM owner.")
    internal_notes: list[str] = Field(default_factory=list, description="Next steps for the CSM, such as escalating a ticket.")


def record_retention_action(action: RetentionAction) -> dict[str, Any]:
    """Record a retention action (outreach email draft and optional offer) for one account. Call at most once per
    account per run. Offer limits and approval are enforced here; a policy violation returns an error to fix."""
    account_id = normalize_account_id(action.account_id)
    account = _cosmos.get_account(account_id)
    if account is None:
        return {"status": "rejected", "error": f"Account {account_id} was not found."}

    if action.decision == "outreach_only" and action.offer_type != "none":
        return {"status": "rejected", "error": "outreach_only must use offer_type 'none'."}
    if action.decision == "retention_offer" and action.offer_type == "none":
        return {"status": "rejected", "error": "retention_offer needs an offer_type other than 'none'."}

    tier = str(account.get("tier", "")).lower()
    offer = evaluate_offer(tier, float(account.get("annual_contract_value") or 0), action.offer_type, action.discount_percent)
    if not offer["allowed"]:
        return {"status": "rejected_by_policy", "error": offer["error"], "max_discount_percent": offer.get("max_discount_percent")}

    # One action per account per usage snapshot: retries and duplicate change feed
    # deliveries for the same change map to the same document id. Without a snapshot
    # id, fall back to one action per account per day.
    now = datetime.now(UTC)
    version = (account.get("usage") or {}).get("snapshot_id") or now.date().isoformat()
    action_id = _UNSAFE_ID_CHARS.sub("_", f"{account_id}--{version}")

    contact = account.get("primary_contact") or {}
    document = {
        "id": action_id,
        "account_id": account_id,
        "account_name": account.get("name"),
        "tier": tier,
        "usage_snapshot_id": version,
        "decision": action.decision,
        "status": "needs_approval" if offer["needs_approval"] else "draft_ready",
        "approval_reasons": offer["approval_reasons"],
        "risk_level": action.risk_level,
        "churn_signals": action.churn_signals,
        "likely_cause": action.likely_cause,
        "offer": {
            "type": action.offer_type,
            "discount_percent": action.discount_percent,
            "annual_value": offer["offer_value"],
            "summary": action.offer_summary,
        },
        "email_draft": {
            # The recipient comes from the account record, never from the model.
            "to": contact.get("email", ""),
            "to_name": contact.get("name", ""),
            "from_owner": account.get("csm_owner", ""),
            "subject": action.email_subject,
            "body": action.email_body,
        },
        "internal_notes": action.internal_notes,
        "renewal_date": account.get("renewal_date"),
        "created_at": now.isoformat(),
    }
    if not _cosmos.create_action_once(document):
        return {"status": "duplicate", "action_id": action_id}
    return {"status": "recorded", "action_id": action_id, "action_status": document["status"]}
