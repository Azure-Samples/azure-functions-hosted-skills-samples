# Copyright (c) Microsoft Corporation.
# Licensed under the MIT License.

"""Vendor contract lookup backed by sample data."""

from __future__ import annotations

from typing import Any

from _sample_data import load


def get_contract_terms(vendor_id: str) -> dict[str, Any]:
    """Get the contract terms for a vendor: price tolerance, freight policy, payment terms, and remit-to details.

    Args:
        vendor_id: ``vendor_id`` from the purchase order, for example ``VEND-100``.
    """
    contract = load("vendor_contracts").get(vendor_id.strip().upper())
    if contract is None:
        return {"error": f"No contract on file for vendor {vendor_id}.", "vendor_id": vendor_id}
    return contract
