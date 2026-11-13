# Copyright (c) Microsoft Corporation.
# Licensed under the MIT License.

"""Three-way match: compares invoice lines with the purchase order, goods receipt, and contract.

The comparison and all arithmetic run in code, so the model explains discrepancies
instead of calculating them.

Do not add ``from __future__ import annotations`` here: the runtime loads tool
files dynamically and pydantic must resolve the model's types at import time.
"""

from decimal import ROUND_HALF_UP, Decimal
from typing import Any

from _sample_data import load
from pydantic import BaseModel, Field

_CENT = Decimal("0.01")


class InvoiceLine(BaseModel):
    sku: str = Field(description="Item code or SKU as printed on the invoice.")
    description: str = Field(default="", description="Line description as printed.")
    quantity: float = Field(description="Billed quantity.")
    unit_price: float = Field(description="Billed unit price.")


class InvoiceToCheck(BaseModel):
    po_number: str = Field(description="PO number printed on the invoice.")
    lines: list[InvoiceLine] = Field(description="Every line item on the invoice.")
    freight: float = Field(default=0, description="Freight or shipping charge; 0 if none.")
    invoice_total: float = Field(description="Total amount due as printed on the invoice.")


def _money(value: Decimal | float) -> float:
    return float(Decimal(str(value)).quantize(_CENT, rounding=ROUND_HALF_UP))


def check_invoice_against_po(invoice: InvoiceToCheck) -> dict[str, Any]:
    """Compare invoice lines, freight, and total with the PO, goods receipt, and contract. Returns every discrepancy and the amount at risk."""
    order = load("purchase_orders").get(invoice.po_number.strip().upper())
    if order is None:
        return {
            "po_found": False,
            "matched": False,
            "discrepancies": [{
                "type": "unknown_po",
                "detail": f"PO {invoice.po_number} does not exist.",
                "amount_at_risk": _money(invoice.invoice_total),
            }],
            "total_amount_at_risk": _money(invoice.invoice_total),
        }

    contract = load("vendor_contracts").get(order["vendor_id"], {})
    tolerance = Decimal(str(contract.get("price_tolerance_pct", 0))) / 100
    max_freight = Decimal(str(contract.get("max_freight", 0)))
    po_lines = {line["sku"].upper(): line for line in order["lines"]}

    discrepancies: list[dict[str, Any]] = []
    line_total = Decimal(0)
    for line in invoice.lines:
        quantity = Decimal(str(line.quantity))
        price = Decimal(str(line.unit_price))
        line_total += quantity * price
        po_line = po_lines.get(line.sku.strip().upper())
        if po_line is None:
            discrepancies.append({
                "type": "line_not_on_po",
                "sku": line.sku,
                "detail": f"{line.sku} is not on {order['po_number']}.",
                "amount_at_risk": _money(quantity * price),
            })
            continue

        agreed = Decimal(str(po_line["unit_price"]))
        received = Decimal(str(po_line["quantity_received"]))
        if price > agreed * (1 + tolerance):
            discrepancies.append({
                "type": "price_above_tolerance",
                "sku": line.sku,
                "detail": (
                    f"Billed {price} vs agreed {agreed} "
                    f"(+{_money((price - agreed) / agreed * 100)}%, tolerance {_money(tolerance * 100)}%)."
                ),
                "amount_at_risk": _money((price - agreed) * min(quantity, received)),
            })
        if quantity > received:
            discrepancies.append({
                "type": "quantity_above_received",
                "sku": line.sku,
                "detail": f"Billed {quantity} but only {received} received.",
                "amount_at_risk": _money((quantity - received) * price),
            })

    freight = Decimal(str(invoice.freight))
    if freight > max_freight:
        discrepancies.append({
            "type": "freight_not_allowed",
            "detail": f"Freight {freight} exceeds the contract limit of {max_freight}. {contract.get('freight_policy', '')}".strip(),
            "amount_at_risk": _money(freight - max_freight),
        })

    computed_total = line_total + freight
    printed_total = Decimal(str(invoice.invoice_total))
    if abs(computed_total - printed_total) > _CENT:
        discrepancies.append({
            "type": "total_mismatch",
            "detail": f"Lines plus freight add up to {_money(computed_total)}, but the invoice total is {_money(printed_total)}.",
            "amount_at_risk": _money(abs(computed_total - printed_total)),
        })

    return {
        "po_found": True,
        "vendor_id": order["vendor_id"],
        "vendor_name": order["vendor_name"],
        "matched": not discrepancies,
        "computed_line_total": _money(line_total),
        "computed_invoice_total": _money(computed_total),
        "invoice_total": _money(printed_total),
        "discrepancies": discrepancies,
        "total_amount_at_risk": _money(sum((Decimal(str(d["amount_at_risk"])) for d in discrepancies), Decimal(0))),
    }
