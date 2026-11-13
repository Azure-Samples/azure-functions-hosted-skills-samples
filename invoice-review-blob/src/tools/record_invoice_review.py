# Copyright (c) Microsoft Corporation.
# Licensed under the MIT License.

"""Records the hosted skill's invoice review for accounts payable.

This tool only stores a recommendation. Payments, holds, and rejections stay in
your ERP behind its own approval process.

Do not add ``from __future__ import annotations`` here: the runtime loads tool
files dynamically and pydantic must resolve the model's types at import time.
"""

import re
from datetime import UTC, datetime
from pathlib import PurePosixPath
from typing import Any, Literal

import _storage
from pydantic import BaseModel, Field

_REVIEW_ID = re.compile(r"^[A-Za-z0-9._-]{1,128}$")


class InvoiceReview(BaseModel):
    blob_name: str = Field(description="Blob name from the trigger data, for example invoices/INV-FAB-1001.pdf.")
    invoice_id: str = Field(description="Invoice number as printed, or the file name without extension if missing.")
    vendor_name: str = Field(default="", description="Vendor name as printed on the invoice.")
    po_number: str = Field(default="", description="PO number as printed on the invoice.")
    invoice_total: float = Field(default=0, description="Invoice total as printed.")
    decision: Literal["approve_for_payment", "hold_for_review", "reject"]
    requires_human_approval: bool = Field(description="true for every decision except approve_for_payment.")
    discrepancies: list[str] = Field(default_factory=list, description="One short line per discrepancy or suspicious item.")
    amount_in_dispute: float = Field(default=0, description="total_amount_at_risk from check_invoice_against_po.")
    reasons: list[str] = Field(description="Short, verifiable reasons tied to the PO, receipt, or contract.")
    evidence: list[str] = Field(default_factory=list, description="Specific facts used, such as receipt IDs and contract clauses.")
    vendor_message_draft: str = Field(default="", description="Message AP can send the vendor; empty for approve_for_payment.")


def record_invoice_review(review: InvoiceReview) -> dict[str, Any]:
    """Record the review of one invoice file for accounts payable. Call once per invoice file."""
    try:
        path = _storage.invoice_path(review.blob_name)
    except ValueError as exc:
        return {"status": "rejected", "error": str(exc)}
    review_id = PurePosixPath(path).stem
    if not _REVIEW_ID.match(review_id):
        return {"status": "rejected", "error": "The invoice file name must use letters, digits, '.', '-' or '_'."}

    # Code enforces the approval rule instead of trusting the model's flag: anything
    # other than a clean match goes to a person.
    requires_approval = (
        review.decision != "approve_for_payment"
        or review.requires_human_approval
        or bool(review.discrepancies)
        or review.amount_in_dispute > 0
    )

    document = review.model_dump()
    document["blob_name"] = path
    document["requires_human_approval"] = requires_approval
    document["recorded_at"] = datetime.now(UTC).isoformat()

    if not _storage.save_review_once(review_id, document):
        return {"status": "duplicate", "review_id": review_id}
    return {"status": "recorded", "review_id": review_id, "requires_human_approval": requires_approval}
