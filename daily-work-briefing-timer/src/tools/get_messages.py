# Copyright (c) Microsoft Corporation.
# Licensed under the MIT License.

"""Unread and important messages for the briefing owner."""

from __future__ import annotations

from datetime import timedelta
from typing import Any

import _context

INTERNAL_DOMAIN = "contoso.com"
_IMPORTANCE_ORDER = {"high": 0, "normal": 1, "low": 2}


def _sender_domain(sender: str) -> str:
    address = sender.rsplit("<", 1)[-1].rstrip(">").strip()
    return address.rsplit("@", 1)[-1].lower() if "@" in address else ""


def get_messages(only_unread: bool = True) -> dict[str, Any]:
    """Get the owner's recent email, most important first. Marks senders outside the company and messages with links.

    Args:
        only_unread: Return only unread messages (default true).
    """
    if _context.office365_enabled():
        return {
            "data_source": "office365",
            "next_step": (
                "Call office365_GetEmailsV3 with folderPath 'Inbox', "
                f"fetchOnlyUnread {str(only_unread).lower()}, importance 'Any' and top 25."
            ),
        }

    now = _context.now()
    messages = []
    for raw in _context.load("messages")["messages"]:
        if only_unread and not raw["unread"]:
            continue
        message = {key: value for key, value in raw.items() if key != "received_minutes_ago"}
        message["received_at"] = (now - timedelta(minutes=raw["received_minutes_ago"])).isoformat(timespec="minutes")
        domain = _sender_domain(raw["from"])
        message["external_sender"] = domain != INTERNAL_DOMAIN
        message["contains_link"] = "http://" in raw["preview"] or "https://" in raw["preview"]
        messages.append(message)
    # Most important first; newest first within the same importance.
    messages.sort(key=lambda m: m["received_at"], reverse=True)
    messages.sort(key=lambda m: _IMPORTANCE_ORDER.get(m["importance"], 1))
    return {"data_source": "sample", "count": len(messages), "messages": messages}
