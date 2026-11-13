"""Open tasks and follow-ups, with due dates and overdue status computed in code."""

from __future__ import annotations

from datetime import timedelta
from typing import Any

import _context

_PRIORITY_ORDER = {"high": 0, "medium": 1, "low": 2}


def get_tasks() -> dict[str, Any]:
    """Get the owner's open tasks and follow-ups, overdue and due-today first."""
    today = _context.briefing_date()
    tasks = []
    for raw in _context.load("tasks")["tasks"]:
        if raw["status"] == "done":
            continue
        due = today + timedelta(days=raw["due_in_days"])
        task = {key: value for key, value in raw.items() if key != "due_in_days"}
        task["due_date"] = due.isoformat()
        task["overdue"] = due < today
        task["due_today"] = due == today
        tasks.append(task)
    tasks.sort(key=lambda t: (t["due_date"], _PRIORITY_ORDER.get(t["priority"], 1)))
    return {
        "data_source": "sample",
        "date": today.isoformat(),
        "overdue": sum(t["overdue"] for t in tasks),
        "due_today": sum(t["due_today"] for t in tasks),
        "tasks": tasks,
    }
