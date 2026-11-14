# Copyright (c) Microsoft Corporation.
# Licensed under the MIT License.

"""Deterministic alert rules: validation, suppression, fingerprints, grouping, and state.

Everything here is pure Python with no I/O so it is cheap to run on every event and easy
to unit test. Tune the rules in ``src/data/suppressions.json`` and ``src/data/assets.json``.
"""

from __future__ import annotations

import hashlib
import json
import re
from datetime import UTC, datetime
from functools import cache
from pathlib import Path
from typing import Any

DATA_DIR = Path(__file__).resolve().parent.parent / "data"

SEVERITIES = ["informational", "low", "medium", "high", "critical"]
_SEVERITY_RANK = {name: rank for rank, name in enumerate(SEVERITIES)}
_ALERT_ID = re.compile(r"^[A-Za-z0-9_.:-]{1,80}$")
_ALERT_FIELDS = ("alert_id", "source", "rule_id", "title", "severity", "host", "user", "detected_at", "description", "evidence")
MAX_DESCRIPTION = 2000
# Short fields are also written to Table Storage (64 KiB per property), so they are bounded.
MAX_TITLE = 500
MAX_NAME = 256


class InvalidAlert(ValueError):
    """The event is not a usable alert. It goes to the review queue, never silently dropped."""


@cache
def load_data(name: str) -> Any:
    with (DATA_DIR / f"{name}.json").open(encoding="utf-8") as f:
        return json.load(f)


def parse_time(value: str) -> datetime:
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)


def parse_alert(body: bytes | str | dict[str, Any], now: datetime | None = None) -> dict[str, Any]:
    """Validate one event body and return a normalized alert dict."""
    if isinstance(body, bytes | bytearray):
        body = body.decode("utf-8", errors="replace")
    if isinstance(body, str):
        try:
            body = json.loads(body)
        except json.JSONDecodeError as exc:
            raise InvalidAlert(f"event body is not JSON: {exc.msg}") from exc
    if not isinstance(body, dict):
        raise InvalidAlert("event body must be a JSON object")

    alert = {key: body.get(key) for key in _ALERT_FIELDS}
    alert_id = str(alert["alert_id"] or "").strip()
    if not _ALERT_ID.match(alert_id):
        raise InvalidAlert("alert_id is missing or has unsupported characters")
    if not str(alert["rule_id"] or "").strip():
        raise InvalidAlert(f"{alert_id}: rule_id is missing")

    alert["alert_id"] = alert_id
    alert["rule_id"] = str(alert["rule_id"]).strip().upper()[:MAX_NAME]
    alert["source"] = str(alert["source"] or "unknown").strip()[:MAX_NAME]
    alert["title"] = str(alert["title"] or alert["rule_id"]).strip()[:MAX_TITLE]
    alert["host"] = str(alert["host"] or "").strip().lower()[:MAX_NAME]
    alert["user"] = str(alert["user"] or "").strip().lower()[:MAX_NAME]
    alert["description"] = str(alert["description"] or "")[:MAX_DESCRIPTION]
    alert["evidence"] = alert["evidence"] if isinstance(alert["evidence"], dict) else {}

    severity = str(alert["severity"] or "").strip().lower()
    # Unknown severities are treated as medium so they still get a model decision.
    alert["severity"] = severity if severity in _SEVERITY_RANK else "medium"

    try:
        alert["detected_at"] = parse_time(str(alert["detected_at"])).isoformat()
    except ValueError:
        alert["detected_at"] = (now or datetime.now(UTC)).isoformat()
    return alert


def entity_of(alert: dict[str, Any]) -> str:
    """The host, or the user when there is no host. Related alerts share an entity."""
    return alert["host"] or alert["user"] or "unknown"


def fingerprint(alert: dict[str, Any]) -> str:
    """Identity of 'the same alert again': source, rule, severity, host, and user — not the alert ID or time.

    Severity is part of the key so an escalation (for example, low to critical) is never
    hidden as a duplicate of the earlier, lower-severity alert.
    """
    key = "|".join([alert["source"].lower(), alert["rule_id"], alert["severity"], alert["host"], alert["user"]])
    return hashlib.sha256(key.encode("utf-8")).hexdigest()[:32]


def window_bucket(alert: dict[str, Any], window_minutes: int) -> int:
    """Fixed dedupe window the alert falls in, as minutes since the epoch divided by the window."""
    minutes = int(parse_time(alert["detected_at"]).timestamp() // 60)
    return minutes // max(1, window_minutes)


def asset_for(alert: dict[str, Any], assets: dict[str, Any] | None = None) -> dict[str, Any]:
    assets = assets if assets is not None else load_data("assets")
    info: dict[str, Any] = {}
    if alert["host"] and alert["host"] in assets.get("hosts", {}):
        info["host"] = {"name": alert["host"], **assets["hosts"][alert["host"]]}
    if alert["user"] and alert["user"] in assets.get("users", {}):
        info["user"] = {"name": alert["user"], **assets["users"][alert["user"]]}
    return info


def match_suppression(
    alert: dict[str, Any],
    suppressions: list[dict[str, Any]] | None = None,
    assets: dict[str, Any] | None = None,
) -> dict[str, Any] | None:
    """Return the known-benign rule that matches the alert, if any.

    A rule matches on ``rule_id`` plus every other condition it sets: ``host``, ``user``,
    or ``asset_tag`` (a tag on the host in the asset inventory). Critical alerts are never
    suppressed.
    """
    if alert["severity"] == "critical":
        return None
    suppressions = suppressions if suppressions is not None else load_data("suppressions")
    host_tags = set(asset_for(alert, assets).get("host", {}).get("tags", []))
    for rule in suppressions:
        if rule.get("rule_id", "").upper() != alert["rule_id"]:
            continue
        if rule.get("host") and rule["host"].lower() != alert["host"]:
            continue
        if rule.get("user") and rule["user"].lower() != alert["user"]:
            continue
        if rule.get("asset_tag") and rule["asset_tag"] not in host_tags:
            continue
        return rule
    return None


def group_alerts(alerts: list[dict[str, Any]]) -> list[list[dict[str, Any]]]:
    """Group alerts in a batch by entity so related alerts are routed and investigated together."""
    groups: dict[str, list[dict[str, Any]]] = {}
    for alert in alerts:
        groups.setdefault(entity_of(alert), []).append(alert)
    return [sorted(group, key=_priority) for group in groups.values()]


def _priority(alert: dict[str, Any]) -> tuple[int, str]:
    return (-_SEVERITY_RANK[alert["severity"]], alert["detected_at"])


def primary(group: list[dict[str, Any]]) -> dict[str, Any]:
    """Highest severity, then earliest. Its alert_id names the group."""
    return min(group, key=_priority)


def max_severity(group: list[dict[str, Any]]) -> str:
    return max((a["severity"] for a in group), key=_SEVERITY_RANK.__getitem__)


def is_critical(group: list[dict[str, Any]]) -> bool:
    return max_severity(group) == "critical"


def build_state(group: list[dict[str, Any]], assets: dict[str, Any] | None = None) -> dict[str, Any]:
    """Code-enriched state for Microsoft-Decision-1: the alerts plus asset context."""
    return {
        "alert_count": len(group),
        "max_severity": max_severity(group),
        "alerts": [
            {key: alert[key] for key in ("rule_id", "title", "severity", "host", "user", "description", "evidence")}
            for alert in group
        ],
        "assets": asset_for(primary(group), assets),
    }
