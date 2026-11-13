"""Identity provider sign-in log lookup backed by sample data."""

from __future__ import annotations

from typing import Any

from _timeline import events_for


def get_signin_logs(user: str, as_of: str = "", lookback_hours: int = 24) -> dict[str, Any]:
    """Get a user's sign-ins before ``as_of``: result, app, IP, location, device, MFA, and follow-up activity.

    Args:
        user: User name, for example ``a.patel``.
        as_of: ISO-8601 time to look back from, usually the alert's ``detected_at``.
        lookback_hours: Hours to look back, 1-168. Defaults to 24.
    """
    return events_for("signin_logs", user, as_of, lookback_hours)
