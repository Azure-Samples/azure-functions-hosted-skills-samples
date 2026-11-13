# Copyright (c) Microsoft Corporation.
# Licensed under the MIT License.

from datetime import datetime
from zoneinfo import ZoneInfo

import _context
import _storage
import pytest
from get_calendar import get_calendar
from get_messages import get_messages
from get_tasks import get_tasks
from pydantic import ValidationError
from publish_briefing import Priority, WorkBriefing, publish_briefing

NOW = datetime(2026, 5, 4, 7, 30, tzinfo=ZoneInfo("America/New_York"))


@pytest.fixture(autouse=True)
def clock(monkeypatch):
    monkeypatch.setenv("BRIEFING_TIMEZONE", "America/New_York")
    monkeypatch.delenv("O365_MCP_SERVER_URL", raising=False)
    monkeypatch.setattr(_context, "now", lambda: NOW)


def test_calendar_finds_conflicts_and_free_blocks():
    result = get_calendar()
    assert result["date"] == "2026-05-04" and result["data_source"] == "sample"
    assert [e["id"] for e in result["events"]] == ["CAL-1", "CAL-2", "CAL-3", "CAL-4", "CAL-5", "CAL-6"]
    assert result["conflicts"] == [{"event_ids": ["CAL-5", "CAL-6"], "overlap_minutes": 15}]
    assert {"start": "09:15", "end": "10:00", "minutes": 45} in result["free_blocks"]
    assert {"start": "13:45", "end": "15:30", "minutes": 105} in result["free_blocks"]


def test_messages_flag_external_senders_and_links():
    result = get_messages()
    ids = [m["id"] for m in result["messages"]]
    assert "MSG-7" not in ids and ids[:3] == ["MSG-3", "MSG-1", "MSG-2"]
    phishing = result["messages"][0]
    assert phishing["external_sender"] is True and phishing["contains_link"] is True
    assert phishing["received_at"] == "2026-05-04T07:00-04:00"
    assert "MSG-7" in [m["id"] for m in get_messages(only_unread=False)["messages"]]


def test_tasks_compute_overdue_and_skip_done():
    result = get_tasks()
    assert [t["id"] for t in result["tasks"]][:3] == ["TASK-2", "TASK-1", "TASK-3"]
    assert "TASK-6" not in [t["id"] for t in result["tasks"]]
    assert result["overdue"] == 1 and result["due_today"] == 2
    assert result["tasks"][0]["due_date"] == "2026-05-03" and result["tasks"][0]["overdue"] is True


def test_office365_mode_points_at_mcp_tools(monkeypatch):
    monkeypatch.setenv("O365_MCP_SERVER_URL", "https://example.azure-apihub.net/mcp")
    calendar = get_calendar()
    assert calendar["data_source"] == "office365"
    assert "office365_GetEventsCalendarViewV3" in calendar["next_step"]
    assert "2026-05-04T04:00:00" in calendar["next_step"] and "2026-05-05T04:00:00" in calendar["next_step"]
    assert "office365_GetEmailsV3" in get_messages()["next_step"]


def test_unresolved_placeholder_is_not_office365(monkeypatch):
    monkeypatch.setenv("O365_MCP_SERVER_URL", "$O365_MCP_SERVER_URL")
    assert _context.data_source() == "sample"


def test_unknown_timezone_falls_back_to_utc(monkeypatch):
    monkeypatch.setenv("BRIEFING_TIMEZONE", "Mars/Olympus")
    assert _context.timezone_name() == "UTC"


@pytest.fixture
def store(monkeypatch):
    saved: dict[str, str] = {}

    def save_once(name, content, content_type):
        if name in saved:
            return False
        saved[name] = content
        return True

    monkeypatch.setattr(_storage, "save_once", save_once)
    return saved


def _briefing(**overrides):
    values = {
        "owner": "Alex Rivera",
        "headline": "Customer escalation at 1 PM; roadmap decision at 10.",
        "top_priorities": [Priority(title="Send the Fabrikam RCA", why="Overdue; call at 13:00.", related_ids=["TASK-2"])],
        "schedule": [{"time": "13:00-13:45", "title": "Fabrikam escalation | call", "prep": "Bring the RCA"}],
        "watch_outs": ["MSG-3 looks like phishing; report it."],
    }
    values.update(overrides)
    return WorkBriefing(**values)


def test_publish_writes_markdown_and_json_once(store):
    assert publish_briefing(_briefing()) == {"status": "published", "date": "2026-05-04", "blob": "briefings/2026-05-04.md"}
    markdown = store["2026-05-04.md"]
    assert markdown.startswith("# Daily work briefing - 2026-05-04")
    assert "1. **Send the Fabrikam RCA** - Overdue; call at 13:00." in markdown
    assert "| 13:00-13:45 | Fabrikam escalation / call | Bring the RCA |" in markdown
    assert '"data_source": "sample"' in store["2026-05-04.json"]
    assert publish_briefing(_briefing(headline="Again"))["status"] == "already_published"
    assert store["2026-05-04.md"] == markdown


def test_briefing_allows_at_most_three_priorities():
    four = [Priority(title=f"P{i}", why="w") for i in range(4)]
    with pytest.raises(ValidationError):
        _briefing(top_priorities=four)
    with pytest.raises(ValidationError):
        _briefing(top_priorities=[])
