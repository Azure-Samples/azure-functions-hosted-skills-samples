# Copyright (c) Microsoft Corporation.
# Licensed under the MIT License.

"""Render a briefing document as Markdown. Deterministic, so reruns produce the same file."""

from __future__ import annotations

from typing import Any


def _bullets(items: list[str]) -> list[str]:
    return [f"- {item}" for item in items] or ["- None"]


def render_markdown(doc: dict[str, Any]) -> str:
    lines = [
        f"# Daily work briefing - {doc['date']}",
        "",
        f"_{doc['owner']} | {doc['timezone']} | data: {doc['data_source']} | generated {doc['generated_at']}_",
        "",
        doc["headline"],
        "",
        "## Top 3 today",
        "",
    ]
    for number, item in enumerate(doc["top_priorities"], start=1):
        lines.append(f"{number}. **{item['title']}** - {item['why']}")
        if item.get("next_step"):
            lines.append(f"   - Next step: {item['next_step']}")
    lines += ["", "## Schedule", ""]
    if doc["schedule"]:
        lines += ["| Time | Meeting | Prep |", "|---|---|---|"]
        for entry in doc["schedule"]:
            prep = (entry.get("prep") or "").replace("|", "/")
            lines.append(f"| {entry['time']} | {entry['title'].replace('|', '/')} | {prep} |")
    else:
        lines.append("- No meetings")
    lines += ["", "## Messages to answer", ""]
    lines += _bullets([f"**{m['sender']}** - {m['subject']}: {m['action']}" for m in doc["messages_to_answer"]])
    lines += ["", "## Follow-ups", ""]
    lines += _bullets(doc["follow_ups"])
    lines += ["", "## Watch out", ""]
    lines += _bullets(doc["watch_outs"])
    return "\n".join(lines) + "\n"
