# Copyright (c) Microsoft Corporation.
# Licensed under the MIT License.

from pathlib import Path

import _storage
import pytest
from check_invoice_against_po import InvoiceLine, InvoiceToCheck, check_invoice_against_po
from extract_invoice import extract_invoice
from get_contract_terms import get_contract_terms
from get_purchase_order import get_purchase_order
from make_samples import INVOICES, build_pdf
from record_invoice_review import InvoiceReview, record_invoice_review

SAMPLES_DIR = Path(__file__).resolve().parent.parent / "samples"


def _invoice(po, lines, freight, total):
    return InvoiceToCheck(
        po_number=po,
        lines=[InvoiceLine(sku=s, quantity=q, unit_price=p) for s, q, p in lines],
        freight=freight,
        invoice_total=total,
    )


def test_lookups():
    assert get_purchase_order("po-4001")["vendor_id"] == "VEND-100"
    assert "error" in get_purchase_order("PO-4099")
    assert get_contract_terms("VEND-200")["max_freight"] == 0
    assert "error" in get_contract_terms("VEND-999")


def test_clean_invoice_matches():
    result = check_invoice_against_po(
        _invoice("PO-4001", [("BRG-6204", 200, 4.80), ("BLT-M8-40", 50, 12.50), ("GLV-NIT-L", 20, 38.00)], 45, 2390)
    )
    assert result["matched"] is True
    assert result["computed_invoice_total"] == 2390.00
    assert result["total_amount_at_risk"] == 0


def test_discrepancies_are_calculated_in_code():
    result = check_invoice_against_po(
        _invoice("PO-4002", [("BOX-241812", 500, 1.85), ("TAPE-48", 30, 57.90)], 220, 2882)
    )
    by_type = {d["type"]: d["amount_at_risk"] for d in result["discrepancies"]}
    assert by_type == {"quantity_above_received": 185.00, "price_above_tolerance": 117.00, "freight_not_allowed": 220.00}
    assert result["total_amount_at_risk"] == 522.00


def test_unknown_po_and_total_mismatch():
    unknown = check_invoice_against_po(_invoice("PO-4099", [("PAPER-A4", 40, 42.50)], 0, 1700))
    assert unknown["po_found"] is False and unknown["total_amount_at_risk"] == 1700.00
    mismatch = check_invoice_against_po(_invoice("PO-4001", [("BRG-6204", 200, 4.80)], 0, 990))
    assert [d["type"] for d in mismatch["discrepancies"]] == ["total_mismatch"]


def test_invoice_path_normalizes_and_blocks_traversal():
    assert _storage.invoice_path("invoices/INV-1.pdf") == "INV-1.pdf"
    assert _storage.invoice_path("INV-1.pdf") == "INV-1.pdf"
    with pytest.raises(ValueError):
        _storage.invoice_path("invoices/../secrets.txt")


def test_committed_samples_match_generator():
    for invoice_id, lines in INVOICES.items():
        assert (SAMPLES_DIR / f"{invoice_id}.pdf").read_bytes() == build_pdf(lines), invoice_id


def test_extract_invoice_reads_sample_pdf_text(monkeypatch):
    monkeypatch.delenv("DOCUMENT_INTELLIGENCE_ENDPOINT", raising=False)
    monkeypatch.setattr(_storage, "read_invoice", lambda name: (SAMPLES_DIR / name).read_bytes())
    result = extract_invoice("invoices/INV-NW-2002.pdf")
    assert result["extraction_method"] == "pdf_text"
    assert "PO-4002" in result["text"] and "2,882.00" in result["text"]
    assert "error" in extract_invoice("invoices/../x.pdf")


@pytest.fixture
def store(monkeypatch):
    saved: dict[str, dict] = {}

    def save_review_once(review_id, document):
        if review_id in saved:
            return False
        saved[review_id] = document
        return True

    monkeypatch.setattr(_storage, "save_review_once", save_review_once)
    return saved


def _review(**overrides):
    values = {
        "blob_name": "invoices/INV-NW-2002.pdf",
        "invoice_id": "INV-NW-2002",
        "decision": "approve_for_payment",
        "requires_human_approval": False,
        "reasons": ["Matches PO-4002."],
    }
    values.update(overrides)
    return InvoiceReview(**values)


def test_record_forces_approval_when_discrepancies_exist(store):
    result = record_invoice_review(_review(discrepancies=["Freight not allowed."], amount_in_dispute=220))
    assert result == {"status": "recorded", "review_id": "INV-NW-2002", "requires_human_approval": True}
    assert store["INV-NW-2002"]["blob_name"] == "INV-NW-2002.pdf"


def test_record_clean_match_and_idempotency(store):
    assert record_invoice_review(_review())["requires_human_approval"] is False
    assert record_invoice_review(_review())["status"] == "duplicate"
    assert record_invoice_review(_review(blob_name="invoices/../x.pdf"))["status"] == "rejected"
