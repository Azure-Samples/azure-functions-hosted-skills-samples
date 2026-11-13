# Copyright (c) Microsoft Corporation.
# Licensed under the MIT License.

"""Shared helpers for the briefing tools: sample data, the briefing clock, and data source.

Replace the ``src/data`` reads with calls to your calendar, mail, and task systems, or
turn on the Office 365 Outlook MCP server (see README). Files whose names start with
``_`` are helpers, not tools.
"""

from __future__ import annotations

import json
import logging
import os
from datetime import UTC, date, datetime, tzinfo
from functools import cache
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

logger = logging.getLogger(__name__)

DATA_DIR = Path(__file__).resolve().parent.parent / "data"


@cache
def load(name: str) -> dict[str, Any]:
    with (DATA_DIR / f"{name}.json").open(encoding="utf-8") as f:
        return json.load(f)


def _configured_timezone() -> str:
    return os.environ.get("BRIEFING_TIMEZONE", "").strip() or "UTC"


def timezone() -> tzinfo:
    """The BRIEFING_TIMEZONE zone (IANA name), or UTC if it is unset or unknown."""
    try:
        return ZoneInfo(_configured_timezone())
    except (ZoneInfoNotFoundError, ValueError):
        logger.warning("Unknown BRIEFING_TIMEZONE %r; using UTC.", _configured_timezone())
        return UTC


def timezone_name() -> str:
    zone = timezone()
    return zone.key if isinstance(zone, ZoneInfo) else "UTC"


def now() -> datetime:
    """Current time in the briefing time zone. Tests patch this."""
    return datetime.now(timezone())


def briefing_date() -> date:
    return now().date()


def office365_enabled() -> bool:
    """True when the Office 365 Outlook MCP server is configured (see src/mcp.json)."""
    url = os.environ.get("O365_MCP_SERVER_URL", "").strip()
    return bool(url) and not url.startswith("$")


def data_source() -> str:
    return "office365" if office365_enabled() else "sample"
