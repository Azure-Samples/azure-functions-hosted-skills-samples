"""Endpoint detection and response (EDR) process and file event lookup backed by sample data."""

from __future__ import annotations

from typing import Any

from _timeline import events_for


def get_endpoint_events(host: str, as_of: str = "", lookback_hours: int = 24) -> dict[str, Any]:
    """Get process, file, and download events on a host before ``as_of``, with command lines and hashes.

    Args:
        host: Host name, for example ``fin-ws-22``.
        as_of: ISO-8601 time to look back from, usually the alert's ``detected_at``.
        lookback_hours: Hours to look back, 1-168. Defaults to 24.
    """
    return events_for("endpoint_events", host, as_of, lookback_hours)
