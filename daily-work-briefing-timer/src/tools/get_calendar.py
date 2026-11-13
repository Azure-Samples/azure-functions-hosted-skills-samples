"""Today's calendar, with overlaps and free blocks computed in code."""

from __future__ import annotations

from datetime import UTC, datetime, time, timedelta
from typing import Any

import _context

WORKDAY_START = time(9, 0)
WORKDAY_END = time(17, 30)
MIN_FOCUS_MINUTES = 30


def _at(day, hhmm: str) -> datetime:
    hour, minute = (int(part) for part in hhmm.split(":"))
    return datetime.combine(day, time(hour, minute), tzinfo=_context.timezone())


def _conflicts(events: list[dict[str, Any]]) -> list[dict[str, Any]]:
    found = []
    for i, first in enumerate(events):
        for second in events[i + 1:]:
            overlap = min(first["_end"], second["_end"]) - max(first["_start"], second["_start"])
            if overlap > timedelta(0):
                found.append({
                    "event_ids": [first["id"], second["id"]],
                    "overlap_minutes": int(overlap.total_seconds() // 60),
                })
    return found


def _free_blocks(day, events: list[dict[str, Any]]) -> list[dict[str, Any]]:
    cursor = _at(day, WORKDAY_START.strftime("%H:%M"))
    end_of_day = _at(day, WORKDAY_END.strftime("%H:%M"))
    blocks = []
    for event in sorted(events, key=lambda e: e["_start"]):
        if event["_start"] - cursor >= timedelta(minutes=MIN_FOCUS_MINUTES):
            blocks.append((cursor, event["_start"]))
        cursor = max(cursor, event["_end"])
    if end_of_day - cursor >= timedelta(minutes=MIN_FOCUS_MINUTES):
        blocks.append((cursor, end_of_day))
    return [
        {"start": s.strftime("%H:%M"), "end": e.strftime("%H:%M"), "minutes": int((e - s).total_seconds() // 60)}
        for s, e in blocks
    ]


def get_calendar() -> dict[str, Any]:
    """Get today's meetings for the briefing owner, with overlapping meetings and free blocks for prep work."""
    day = _context.briefing_date()
    base = {"date": day.isoformat(), "timezone": _context.timezone_name()}

    if _context.office365_enabled():
        start = _at(day, "00:00").astimezone(UTC)
        end = (start + timedelta(days=1))
        return {
            **base,
            "data_source": "office365",
            "next_step": (
                "Call office365_GetEventsCalendarViewV3 with calendarId 'Calendar', "
                f"startDateTimeUtc '{start:%Y-%m-%dT%H:%M:%S}' and endDateTimeUtc '{end:%Y-%m-%dT%H:%M:%S}'. "
                "Convert the returned UTC times to the time zone above."
            ),
        }

    data = _context.load("calendar")
    events = []
    for raw in data["events"]:
        event = dict(raw)
        event["_start"], event["_end"] = _at(day, raw["start"]), _at(day, raw["end"])
        event["duration_minutes"] = int((event["_end"] - event["_start"]).total_seconds() // 60)
        events.append(event)
    events.sort(key=lambda e: e["_start"])

    conflicts = _conflicts(events)
    free = _free_blocks(day, events)
    for event in events:
        del event["_start"], event["_end"]
    return {
        **base,
        "data_source": "sample",
        "owner": data["owner"],
        "events": events,
        "conflicts": conflicts,
        "free_blocks": free,
    }
