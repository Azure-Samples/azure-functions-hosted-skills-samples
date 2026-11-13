"""Threat intelligence lookup backed by sample data."""

from __future__ import annotations

from typing import Any

from _sample_data import load


def lookup_threat_intel(indicator: str) -> dict[str, Any]:
    """Look up an IP address, domain, or SHA-256 file hash in threat intelligence.

    Args:
        indicator: The indicator exactly as seen in the evidence, for example ``185.220.101.47``.
    """
    key = indicator.strip().lower()
    match = load("threat_intel").get(key)
    if match is None:
        return {"indicator": indicator, "verdict": "unknown", "detail": "No threat intelligence for this indicator."}
    return {"indicator": key, **match}
