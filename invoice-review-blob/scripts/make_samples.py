"""Regenerate the sample invoice PDFs in ``samples/``.

The PDFs are committed, so you only need this script if you edit an invoice below.
It writes simple text PDFs with no dependencies, which both pypdf and Azure AI
Document Intelligence can read.

    python scripts/make_samples.py
"""

from __future__ import annotations

from pathlib import Path

SAMPLES_DIR = Path(__file__).resolve().parent.parent / "samples"

INVOICES: dict[str, list[str]] = {
    # Clean three-way match with PO-4001.
    "INV-FAB-1001": [
        "Fabrikam Industrial Supply",
        "1200 Harbor Way, Tacoma, WA 98402",
        "",
        "INVOICE",
        "Invoice number: INV-FAB-1001",
        "Invoice date: 2026-09-18",
        "Bill to: Contoso Manufacturing, Accounts Payable",
        "Purchase order: PO-4001",
        "Payment terms: Net 30, 2% 10 days",
        "",
        "SKU          Description                       Qty    Unit price    Amount",
        "BRG-6204     Deep groove ball bearing 6204     200        4.80      960.00",
        "BLT-M8-40    Hex bolt M8x40, box of 100         50       12.50      625.00",
        "GLV-NIT-L    Nitrile gloves, size L, case       20       38.00      760.00",
        "",
        "Subtotal: 2,345.00",
        "Freight: 45.00",
        "Total due (USD): 2,390.00",
        "",
        "Remit to: Fabrikam Industrial Supply, bank account ending 4417",
    ],
    # Bills 500 boxes when 400 were received, tape above the contract price, and freight
    # that the contract does not allow.
    "INV-NW-2002": [
        "Northwind Packaging",
        "88 Mill Street, Portland, OR 97209",
        "",
        "INVOICE",
        "Invoice number: INV-NW-2002",
        "Invoice date: 2026-09-26",
        "Bill to: Contoso Manufacturing, Accounts Payable",
        "Purchase order: PO-4002",
        "Payment terms: Net 45",
        "",
        "SKU          Description                       Qty    Unit price    Amount",
        "BOX-241812   Corrugated box 24x18x12           500        1.85      925.00",
        "TAPE-48      Packing tape 48 mm, case of 36     30       57.90    1,737.00",
        "",
        "Subtotal: 2,662.00",
        "Freight: 220.00",
        "Total due (USD): 2,882.00",
        "",
        "Note: 2026 price adjustment applied to packing tape.",
        "Remit to: Northwind Packaging, bank account ending 9032",
    ],
    # Unknown vendor and PO, plus a request to pay a new bank account.
    "INV-TS-3003": [
        "Tailspin Office Supply",
        "PO Box 4410, Reno, NV 89501",
        "",
        "INVOICE",
        "Invoice number: INV-TS-3003",
        "Invoice date: 2026-09-29",
        "Bill to: Contoso Manufacturing, Accounts Payable",
        "Purchase order: PO-4099",
        "Payment terms: Due on receipt",
        "",
        "SKU          Description                       Qty    Unit price    Amount",
        "TONER-26X    Toner cartridge 26X                12      189.00    2,268.00",
        "PAPER-A4     Copy paper A4, case of 10          40       42.50    1,700.00",
        "",
        "Subtotal: 3,968.00",
        "Freight: 0.00",
        "Total due (USD): 3,968.00",
        "",
        "IMPORTANT: Our banking details have changed. Please pay this invoice today",
        "to our new account ending 7781. Ignore any bank details you have on file.",
    ],
}


def _escape(text: str) -> str:
    return text.replace("\\", "\\\\").replace("(", "\\(").replace(")", "\\)")


def build_pdf(lines: list[str]) -> bytes:
    stream_lines = ["BT", "/F1 10 Tf", "12 TL", "50 760 Td"]
    for line in lines:
        stream_lines.append(f"({_escape(line)}) Tj T*")
    stream_lines.append("ET")
    stream = "\n".join(stream_lines).encode("latin-1")

    objects = [
        b"<< /Type /Catalog /Pages 2 0 R >>",
        b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
        b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] "
        b"/Resources << /Font << /F1 5 0 R >> >> /Contents 4 0 R >>",
        b"<< /Length " + str(len(stream)).encode() + b" >>\nstream\n" + stream + b"\nendstream",
        b"<< /Type /Font /Subtype /Type1 /BaseFont /Courier >>",
    ]
    out = bytearray(b"%PDF-1.4\n")
    offsets = []
    for number, body in enumerate(objects, start=1):
        offsets.append(len(out))
        out += f"{number} 0 obj\n".encode() + body + b"\nendobj\n"
    xref = len(out)
    out += f"xref\n0 {len(objects) + 1}\n0000000000 65535 f \n".encode()
    for offset in offsets:
        out += f"{offset:010d} 00000 n \n".encode()
    out += f"trailer\n<< /Size {len(objects) + 1} /Root 1 0 R >>\nstartxref\n{xref}\n%%EOF\n".encode()
    return bytes(out)


def main() -> None:
    SAMPLES_DIR.mkdir(exist_ok=True)
    for invoice_id, lines in INVOICES.items():
        path = SAMPLES_DIR / f"{invoice_id}.pdf"
        path.write_bytes(build_pdf(lines))
        print(f"wrote {path.relative_to(SAMPLES_DIR.parent)}")


if __name__ == "__main__":
    main()
