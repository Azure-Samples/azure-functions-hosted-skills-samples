# Copyright (c) Microsoft Corporation.
# Licensed under the MIT License.

"""Deterministic claim checks and the decision rule.

Pure functions with no Azure dependencies, so they are easy to unit test and the
workflow tools stay thin. Replace the sample data in ``src/data`` with your policy
administration, document management, and claims systems.
"""

from __future__ import annotations

import json
import os
from collections.abc import Callable
from datetime import date, timedelta
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation
from functools import cache
from pathlib import Path
from typing import Any

DATA_DIR = Path(__file__).resolve().parent.parent / "data"

DECISIONS = ("approve", "request_info", "refer_to_adjuster")
_STRICTNESS = {decision: rank for rank, decision in enumerate(DECISIONS)}

NEW_POLICY_DAYS = 30
LATE_REPORT_DAYS = 30
FREQUENT_CLAIMS_WINDOW_DAYS = 365
FREQUENT_CLAIMS_THRESHOLD = 2
CENT = Decimal("0.01")


class ClaimError(ValueError):
    """The claim form is missing required fields or has invalid values."""


@cache
def load(name: str) -> dict[str, Any]:
    with (DATA_DIR / f"{name}.json").open(encoding="utf-8") as f:
        return json.load(f)


def auto_approve_limit() -> Decimal:
    try:
        return Decimal(os.environ.get("CLAIMS_AUTO_APPROVE_LIMIT", "10000"))
    except InvalidOperation:
        return Decimal("10000")


def money(value: Decimal) -> float:
    return float(value.quantize(CENT, rounding=ROUND_HALF_UP))


def _date(claim: dict[str, Any], field: str) -> date:
    value = claim.get(field)
    try:
        return date.fromisoformat(str(value))
    except ValueError as exc:
        raise ClaimError(f"{field} must be an ISO date (YYYY-MM-DD), got {value!r}.") from exc


def normalize_claim(claim: dict[str, Any]) -> dict[str, Any]:
    """Validate a claim form and return the fields the checks use. Raises ``ClaimError``."""
    errors: list[str] = []
    for field in ("claim_id", "policy_number", "claim_type"):
        if not isinstance(claim.get(field), str) or not claim[field].strip():
            errors.append(f"{field} is required.")
    claimant = claim.get("claimant")
    if not isinstance(claimant, dict) or not str(claimant.get("name", "")).strip():
        errors.append("claimant.name is required.")
    amount: Decimal | None = None
    try:
        amount = Decimal(str(claim.get("amount_claimed")))
        if not amount.is_finite() or amount <= 0:
            errors.append("amount_claimed must be a positive number.")
    except InvalidOperation:
        errors.append("amount_claimed must be a positive number.")
    documents = claim.get("documents", [])
    if not isinstance(documents, list) or not all(
        isinstance(d, dict) and isinstance(d.get("type"), str) and isinstance(d.get("file"), str) for d in documents
    ):
        errors.append("documents must be a list of {type, file} objects.")
        documents = []
    incident = reported = None
    try:
        incident = _date(claim, "incident_date")
        reported = _date(claim, "reported_date") if claim.get("reported_date") else incident
        if reported < incident:
            errors.append("reported_date is before incident_date.")
    except ClaimError as exc:
        errors.append(str(exc))
    if errors:
        raise ClaimError(" ".join(errors))
    return {
        "claim_id": claim["claim_id"].strip(),
        "policy_number": claim["policy_number"].strip().upper(),
        "claim_type": claim["claim_type"].strip().lower(),
        "claimant": str(claimant["name"]).strip(),
        "incident_date": incident,
        "reported_date": reported,
        "amount_claimed": amount,
        "documents": [{"type": d["type"].strip().lower(), "file": d["file"].strip()} for d in documents],
    }


def check_coverage(claim: dict[str, Any], policies: dict[str, Any] | None = None) -> dict[str, Any]:
    policies = load("policies") if policies is None else policies
    policy = policies.get(claim["policy_number"])
    result: dict[str, Any] = {
        "check": "coverage",
        "claim_id": claim["claim_id"],
        "policy_number": claim["policy_number"],
        "claim_type": claim["claim_type"],
        "amount_claimed": money(claim["amount_claimed"]),
        "policy_found": policy is not None,
        "issues": [],
    }
    if policy is None:
        result["issues"].append(f"Policy {claim['policy_number']} was not found.")
        result.update(covered=False, estimated_payout=0.0)
        return result

    effective = date.fromisoformat(policy["effective_date"])
    expiry = date.fromisoformat(policy["expiry_date"])
    coverage = policy.get("coverages", {}).get(claim["claim_type"])
    result.update(
        holder=policy["holder"],
        policy_status=policy["status"],
        effective_date=policy["effective_date"],
        expiry_date=policy["expiry_date"],
    )
    if policy["status"] != "active":
        result["issues"].append(f"Policy status is {policy['status']}.")
    if not effective <= claim["incident_date"] <= expiry:
        result["issues"].append(
            f"Incident date {claim['incident_date']} is outside the policy period {effective} to {expiry}."
        )
    payout = Decimal(0)
    if coverage is None:
        result["issues"].append(f"Claim type {claim['claim_type']} is not covered by this policy.")
    else:
        limit = Decimal(str(coverage["limit"]))
        deductible = Decimal(str(coverage["deductible"]))
        payout = max(Decimal(0), min(claim["amount_claimed"], limit) - deductible)
        result.update(limit=money(limit), deductible=money(deductible))
        if claim["amount_claimed"] > limit:
            result["issues"].append(f"Amount claimed exceeds the coverage limit of {money(limit):,.2f}.")
        if payout == 0:
            result["issues"].append("Amount claimed does not exceed the deductible.")
    result.update(covered=not result["issues"], estimated_payout=money(payout))
    return result


def check_documents(
    claim: dict[str, Any],
    exists: Callable[[str], bool],
    requirements: dict[str, Any] | None = None,
) -> dict[str, Any]:
    requirements = load("document_requirements") if requirements is None else requirements
    required = requirements.get(claim["claim_type"])
    provided = {d["type"]: d["file"] for d in claim["documents"]}
    result: dict[str, Any] = {
        "check": "documents",
        "claim_id": claim["claim_id"],
        "claim_type": claim["claim_type"],
        "required": [r["type"] for r in required or []],
        "provided": sorted(provided),
        "missing": [],
        "issues": [],
    }
    if required is None:
        result["issues"].append(f"No document checklist exists for claim type {claim['claim_type']}.")
    for item in required or []:
        file = provided.get(item["type"])
        if file is None:
            result["missing"].append({**item, "reason": "not listed on the claim form"})
        elif not exists(file):
            result["missing"].append({**item, "reason": f"listed file {file} was not uploaded"})
    result["complete"] = not result["missing"] and not result["issues"]
    return result


def check_claim_history(
    claim: dict[str, Any],
    policies: dict[str, Any] | None = None,
    history: dict[str, Any] | None = None,
) -> dict[str, Any]:
    policies = load("policies") if policies is None else policies
    history = load("claim_history") if history is None else history
    policy = policies.get(claim["policy_number"])
    customer_id = policy.get("customer_id") if policy else None
    prior = history.get("prior_claims", {}).get(customer_id, []) if customer_id else []
    window_start = claim["incident_date"] - timedelta(days=FREQUENT_CLAIMS_WINDOW_DAYS)
    recent = [p for p in prior if window_start <= date.fromisoformat(p["date"]) <= claim["incident_date"]]

    signals: list[dict[str, str]] = []
    if customer_id and customer_id in history.get("siu_watchlist", []):
        signals.append({"code": "siu_watchlist", "detail": f"Customer {customer_id} is on the SIU watchlist."})
    if len(recent) >= FREQUENT_CLAIMS_THRESHOLD:
        signals.append({
            "code": "frequent_claims",
            "detail": f"{len(recent)} prior claims in the 12 months before the incident.",
        })
    if policy:
        effective = date.fromisoformat(policy["effective_date"])
        days = (claim["incident_date"] - effective).days
        if 0 <= days < NEW_POLICY_DAYS:
            signals.append({"code": "new_policy", "detail": f"Incident {days} days after the policy started."})
    delay = (claim["reported_date"] - claim["incident_date"]).days
    if delay > LATE_REPORT_DAYS:
        signals.append({"code": "late_reporting", "detail": f"Reported {delay} days after the incident."})
    for p in prior:
        if p["date"] == claim["incident_date"].isoformat() and p["type"] == claim["claim_type"]:
            signals.append({
                "code": "possible_duplicate",
                "detail": f"Prior claim {p['claim_id']} has the same date and type.",
            })
    return {
        "check": "claim_history",
        "claim_id": claim["claim_id"],
        "customer_id": customer_id,
        "prior_claims_total": len(prior),
        "prior_claims_12_months": recent,
        "fraud_signals": signals,
        "clean": not signals,
    }


CHECKS = {"coverage": "coverage", "documents": "documents", "history": "claim_history"}
"""Workflow argument name -> check name, for the three check results the publisher receives."""

DECISION_FIELDS = {
    "coverage": ("covered", "estimated_payout", "issues"),
    "documents": ("complete", "missing", "issues"),
    "claim_history": ("fraud_signals",),
}
"""Fields of each check result that the decision rule depends on."""


def run_checks(claim: dict[str, Any], exists: Callable[[str], bool]) -> dict[str, dict[str, Any]]:
    """Run all three checks for a normalized claim, keyed by workflow argument name."""
    return {
        "coverage": check_coverage(claim),
        "documents": check_documents(claim, exists),
        "history": check_claim_history(claim),
    }


def find_check_mismatches(reported: dict[str, Any], recomputed: dict[str, dict[str, Any]]) -> list[str]:
    """Compare check results passed through the workflow with the ones recomputed in code.

    Returns one message per argument whose decision-relevant fields differ, or that is
    missing, malformed, or belongs to another claim.
    """
    mismatches: list[str] = []
    for arg, check in CHECKS.items():
        expected = recomputed[arg]
        value = reported.get(arg)
        if not isinstance(value, dict) or value.get("check") != check:
            mismatches.append(f"{arg} is not a {check} check result.")
        elif value.get("claim_id") != expected["claim_id"]:
            mismatches.append(f"{arg} belongs to claim {value.get('claim_id')!r}, not {expected['claim_id']!r}.")
        else:
            fields = [f for f in DECISION_FIELDS[check] if value.get(f) != expected.get(f)]
            if fields:
                mismatches.append(f"{arg} differs from the recomputed {check} check in: {', '.join(fields)}.")
    return mismatches


def decide(
    claim: dict[str, Any],
    coverage: dict[str, Any],
    documents: dict[str, Any],
    history: dict[str, Any],
    recommended: str | None = None,
    limit: Decimal | None = None,
    mismatches: list[str] | None = None,
) -> dict[str, Any]:
    """Apply the decision rule. The strictest outcome wins.

    Fraud signals, a coverage problem, an amount above the auto-approve limit, or check
    results that do not match the recomputed checks always refer the claim to an adjuster.
    Missing documents request information. A model recommendation can only make the
    outcome stricter, never more lenient.
    """
    limit = auto_approve_limit() if limit is None else limit
    refer: list[str] = [f"Check mismatch: {m}" for m in mismatches or []]
    info: list[str] = []
    if history["fraud_signals"]:
        refer.extend(f"Fraud signal ({s['code']}): {s['detail']}" for s in history["fraud_signals"])
    if claim["amount_claimed"] > limit:
        refer.append(
            f"Amount claimed {money(claim['amount_claimed']):,.2f} is above the auto-approve limit {money(limit):,.2f}."
        )
    refer.extend(f"Coverage: {issue}" for issue in coverage["issues"])
    refer.extend(f"Documents: {issue}" for issue in documents["issues"])
    info.extend(f"Missing {m['type']} ({m['description']}): {m['reason']}." for m in documents["missing"])

    decision = "refer_to_adjuster" if refer else "request_info" if info else "approve"
    reasons = refer + info
    if recommended in _STRICTNESS and _STRICTNESS[recommended] > _STRICTNESS[decision]:
        decision = recommended
        reasons.append(f"Intake review recommended {recommended}.")
    if decision == "approve":
        reasons.append(
            "Policy active and covers the loss, all required documents present, no fraud signals, "
            f"amount within the {money(limit):,.2f} auto-approve limit."
        )
    return {
        "decision": decision,
        "reasons": reasons,
        "information_requested": [f"{m['type']}: {m['description']}" for m in documents["missing"]],
        "auto_approved": decision == "approve",
        "requires_human_review": decision != "approve",
    }
