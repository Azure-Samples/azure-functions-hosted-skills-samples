# Copyright (c) Microsoft Corporation.
# Licensed under the MIT License.

"""Deterministic validation, context lookup, and purchasing policy.

This module has no model or Azure dependencies. Catalog data is illustrative;
replace these lookups with authenticated purchasing/inventory APIs.
"""

from __future__ import annotations

import hashlib
import json
import re
from datetime import date
from pathlib import Path
from typing import Any

DATA_DIR = Path(__file__).parent / "data"
DECISIONS = {"accept_update", "buyer_review", "request_clarification"}


def identifier(value: Any, field: str) -> str:
    if not isinstance(value, str) or re.fullmatch(r"[A-Za-z0-9_-]{1,80}", value) is None:
        raise ValueError(f"{field} must contain 1-80 letters, digits, hyphens, or underscores")
    return value


def validate_update(value: Any) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise ValueError("Supplier update must be a JSON object")
    fields = {
        "change_id", "supplier_id", "po_id", "change_kind", "proposed_delivery_date",
        "quantity_available", "substitute_sku", "explanation",
    }
    if set(value) != fields:
        raise ValueError("Supplier update has missing or unknown fields")
    update = dict(value)
    for field in ("change_id", "supplier_id", "po_id"):
        identifier(update[field], field)
    if update["change_kind"] not in ("schedule_update", "exception"):
        raise ValueError("change_kind must be schedule_update or exception")
    proposed = update["proposed_delivery_date"]
    if proposed is not None:
        if not isinstance(proposed, str):
            raise ValueError("proposed_delivery_date must be an ISO date or null")
        try:
            parsed = date.fromisoformat(proposed)
        except ValueError:
            raise ValueError("proposed_delivery_date must be an ISO date or null") from None
        if parsed.isoformat() != proposed:
            raise ValueError("proposed_delivery_date must use YYYY-MM-DD")
    quantity = update["quantity_available"]
    if type(quantity) is not int or not 0 <= quantity <= 1_000_000:
        raise ValueError("quantity_available must be an integer between 0 and 1000000")
    if update["substitute_sku"] is not None:
        identifier(update["substitute_sku"], "substitute_sku")
    explanation = update["explanation"]
    if not isinstance(explanation, str) or not explanation.strip() or len(explanation) > 10_000:
        raise ValueError("explanation must contain 1-10000 characters")
    return update


def fingerprint(update: dict[str, Any]) -> str:
    return hashlib.sha256(json.dumps(update, sort_keys=True).encode("utf-8")).hexdigest()


def load_context(update: dict[str, Any]) -> dict[str, Any]:
    catalog = json.loads((DATA_DIR / "purchasing.json").read_text(encoding="utf-8"))
    po = catalog["purchase_orders"].get(update["po_id"])
    if po is None:
        raise ValueError("Unknown purchase order")
    if po["supplier_id"] != update["supplier_id"]:
        raise ValueError("Supplier does not match the purchase order")
    inventory = catalog["inventory"][po["sku"]]
    policy = catalog["policy"]
    proposed = update["proposed_delivery_date"]
    # Compute dates and quantities in code, never ask the model to do arithmetic.
    delta = (
        (date.fromisoformat(proposed) - date.fromisoformat(inventory["coverage_until"])).days
        if proposed else None
    )
    return {
        "purchase_order": po,
        "inventory": inventory,
        "policy": policy,
        "facts": {
            "shortfall_units": max(0, po["outstanding_quantity"] - update["quantity_available"]),
            "inventory_gap_days": max(0, delta) if delta is not None else None,
            "after_required_date": proposed > po["required_by"] if proposed else None,
            "substitution_approved": update["substitute_sku"] in po["approved_substitutions"],
        },
    }


def policy_reasons(update: dict[str, Any], context: dict[str, Any]) -> list[str]:
    facts = context["facts"]
    reasons = []
    if update["proposed_delivery_date"] is None:
        reasons.append("A confirmed delivery date is missing.")
    if facts["after_required_date"]:
        reasons.append("Delivery is after the purchase order's required date.")
    if facts["inventory_gap_days"]:
        reasons.append(f"Inventory coverage gap: {facts['inventory_gap_days']} days.")
    if facts["shortfall_units"]:
        reasons.append(f"Partial delivery leaves {facts['shortfall_units']} units outstanding.")
    if update["quantity_available"] > context["purchase_order"]["outstanding_quantity"]:
        reasons.append("Proposed quantity exceeds the outstanding order quantity.")
    if update["substitute_sku"] and not facts["substitution_approved"]:
        reasons.append("Substitute part is not on the purchase order's approved list.")
    return reasons


def routine_assessment(update: dict[str, Any], context: dict[str, Any]) -> dict[str, Any] | None:
    # Only an explicitly structured schedule confirmation can take the fast path.
    # Free-text exception reports always go to the skill, even if quantities match.
    if (
        update["change_kind"] == "schedule_update"
        and update["substitute_sku"] is None
        and not policy_reasons(update, context)
    ):
        return {
            "recommendation": "accept_update",
            "summary": "Schedule update stays within the required date and inventory coverage.",
            "reasons": ["Full outstanding quantity is confirmed with no substitution."],
            "questions": [],
        }
    return None


def build_prompt(update: dict[str, Any], context: dict[str, Any]) -> str:
    return json.dumps({"supplier_update": update, "purchasing_context": context}, ensure_ascii=False)


def make_record(
    update: dict[str, Any], context: dict[str, Any], assessment: dict[str, Any], *, used_skill: bool,
) -> dict[str, Any]:
    # Defend the action boundary even if called outside the runtime schema validator.
    if not isinstance(assessment, dict) or assessment.get("recommendation") not in DECISIONS:
        raise ValueError("Invalid assessment recommendation")
    if not isinstance(assessment.get("summary"), str) or not assessment["summary"].strip():
        raise ValueError("Assessment summary is required")
    for field in ("reasons", "questions"):
        values = assessment.get(field)
        if not isinstance(values, list) or not all(isinstance(item, str) and item.strip() for item in values):
            raise ValueError(f"Assessment {field} must be a list of nonblank strings")
    if not assessment["reasons"]:
        raise ValueError("Assessment must include reasons")
    enforced = policy_reasons(update, context)
    recommendation = assessment["recommendation"]
    decision = "buyer_review" if enforced else recommendation
    return {
        "change_id": update["change_id"],
        "po_id": update["po_id"],
        "supplier_id": update["supplier_id"],
        "input_fingerprint": fingerprint(update),
        "used_skill": used_skill,
        "model_recommendation": recommendation if used_skill else None,
        "decision": decision,
        "requires_human_approval": decision != "accept_update",
        "summary": assessment["summary"],
        "reasons": assessment["reasons"],
        "questions": assessment["questions"],
        "policy_reasons": enforced,
        "facts": context["facts"],
        # Snapshot the deterministic evidence so later policy/data edits do not
        # rewrite the meaning of an already recorded recommendation.
        "context": context,
    }


def verify_replay(record: dict[str, Any], update: dict[str, Any]) -> None:
    if record.get("input_fingerprint") != fingerprint(update):
        raise ValueError("change_id already exists with different input; send a new change_id")