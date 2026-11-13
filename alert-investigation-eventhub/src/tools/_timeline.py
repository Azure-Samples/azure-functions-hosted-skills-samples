# Copyright (c) Microsoft Corporation.
# Licensed under the MIT License.

"""Turns the sample logs' relative offsets into timestamps around the alert time.

The sample data stores ``offset_minutes`` relative to the alert so the story stays
consistent whenever the demo runs. Replace these helpers with real log queries
(for example, Microsoft Sentinel or Defender XDR) bounded by the same time window.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

from _sample_data import load


def parse_as_of(as_of: str) -> datetime:
    if not as_of:
        return datetime.now(UTC)
    try:
        parsed = datetime.fromisoformat(as_of.replace("Z", "+00:00"))
    except ValueError:
        return datetime.now(UTC)
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)


def events_for(dataset: str, key: str, as_of: str, lookback_hours: int) -> dict[str, Any]:
    key = key.strip().lower()
    anchor = parse_as_of(as_of)
    lookback = max(1, min(int(lookback_hours or 24), 168))
    events = []
    for item in load(dataset).get(key, []):
        offset = int(item.get("offset_minutes", 0))
        if -offset > lookback * 60:
            continue
        event = {k: v for k, v in item.items() if k != "offset_minutes"}
        events.append({"time": (anchor + timedelta(minutes=offset)).isoformat(), **event})
    events.sort(key=lambda e: e["time"])
    return {
        "key": key,
        "window_start": (anchor - timedelta(hours=lookback)).isoformat(),
        "window_end": anchor.isoformat(),
        "count": len(events),
        "events": events,
    }
