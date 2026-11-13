"""Network flow lookup backed by sample data."""

from __future__ import annotations

from typing import Any

from _timeline import events_for


def get_network_flows(host: str, as_of: str = "", lookback_hours: int = 24) -> dict[str, Any]:
    """Get a host's network connections before ``as_of``: remote IP and port, protocol, bytes, and process.

    Args:
        host: Host name, for example ``fin-ws-22``.
        as_of: ISO-8601 time to look back from, usually the alert's ``detected_at``.
        lookback_hours: Hours to look back, 1-168. Defaults to 24.
    """
    return events_for("network_flows", host, as_of, lookback_hours)
