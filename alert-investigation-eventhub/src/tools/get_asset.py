"""Asset and owner inventory lookup backed by sample data."""

from __future__ import annotations

from typing import Any

from _sample_data import load


def get_asset(name: str) -> dict[str, Any]:
    """Get the inventory record for a host or a user: owner, department, criticality, tags, and roles.

    Args:
        name: Host name (for example ``fin-ws-22``) or user name (for example ``a.patel``).
    """
    key = name.strip().lower()
    assets = load("assets")
    if key in assets["hosts"]:
        return {"kind": "host", "name": key, **assets["hosts"][key]}
    if key in assets["users"]:
        return {"kind": "user", "name": key, **assets["users"][key]}
    return {"error": f"{name} is not in the asset inventory.", "name": name}
