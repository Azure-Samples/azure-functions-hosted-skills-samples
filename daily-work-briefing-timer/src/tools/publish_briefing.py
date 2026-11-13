# Copyright (c) Microsoft Corporation.
# Licensed under the MIT License.

"""Publishes the day's briefing to Blob Storage as Markdown and JSON, once per date.

The date, file names, and rendering are decided in code, not by the model, so a
retried or manual run on the same day returns ``already_published`` instead of
overwriting the briefing someone may already have read.

Do not add ``from __future__ import annotations`` here: the runtime loads tool
files dynamically and pydantic must resolve the model's types at import time.
"""

import json
import logging
from datetime import UTC, datetime
from typing import Any

import _briefing
import _context
import _storage
from pydantic import BaseModel, Field

logger = logging.getLogger(__name__)


class Priority(BaseModel):
    title: str = Field(description="Short imperative, for example 'Send the Fabrikam RCA before 1 PM'.")
    why: str = Field(description="Why this matters today, tied to a meeting, message, or task.")
    next_step: str = Field(default="", description="The first concrete action, with a time if possible.")
    related_ids: list[str] = Field(default_factory=list, description="Event, message, or task IDs this is based on.")


class ScheduleItem(BaseModel):
    time: str = Field(description="Local start-end time, for example '10:00-11:00'.")
    title: str
    prep: str = Field(default="", description="Suggested preparation, or empty if none is needed.")


class MessageToAnswer(BaseModel):
    sender: str
    subject: str
    action: str = Field(description="What to reply or do, in one sentence.")


class WorkBriefing(BaseModel):
    owner: str = Field(default="", description="Who the briefing is for, as returned by the calendar.")
    headline: str = Field(description="One or two sentences that sum up the day.")
    top_priorities: list[Priority] = Field(min_length=1, max_length=3, description="The top 3 (at most) for today, most important first.")
    schedule: list[ScheduleItem] = Field(default_factory=list, description="Today's meetings in time order.")
    messages_to_answer: list[MessageToAnswer] = Field(default_factory=list)
    follow_ups: list[str] = Field(default_factory=list, description="Open tasks and promises to close, overdue first.")
    watch_outs: list[str] = Field(default_factory=list, description="Conflicts, risks, and suspicious messages to ignore or report.")


def publish_briefing(briefing: WorkBriefing) -> dict[str, Any]:
    """Publish today's work briefing. Call exactly once, after you have read the calendar, messages, and tasks."""
    day = _context.briefing_date().isoformat()
    document = briefing.model_dump()
    document.update({
        "date": day,
        "timezone": _context.timezone_name(),
        "data_source": _context.data_source(),
        "owner": briefing.owner or "Briefing owner",
        "generated_at": datetime.now(UTC).isoformat(timespec="seconds"),
    })
    markdown = _briefing.render_markdown(document)
    document["markdown"] = markdown

    container = _storage.container_name()
    if not _storage.save_once(f"{day}.md", markdown, "text/markdown; charset=utf-8"):
        logger.info("Briefing for %s was already published to %s/%s.md", day, container, day)
        return {"status": "already_published", "date": day, "blob": f"{container}/{day}.md"}
    _storage.save_once(f"{day}.json", json.dumps(document, indent=2), "application/json")
    logger.info("Published briefing %s/%s.md\n%s", container, day, markdown)
    return {"status": "published", "date": day, "blob": f"{container}/{day}.md"}
