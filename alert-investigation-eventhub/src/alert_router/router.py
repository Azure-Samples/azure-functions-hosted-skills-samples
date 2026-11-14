# Copyright (c) Microsoft Corporation.
# Licensed under the MIT License.

"""Route a batch of alerts: rules first, Microsoft-Decision-1 for the rest, fail safe always.

Every alert ends in exactly one place:

* suppressed by a known-benign rule  -> recorded in the decisions table
* duplicate of an alert in the window -> counted on the first alert's fingerprint
* critical                            -> ``alerts-investigate`` without a model call
* Decision-1 ``investigate``          -> ``alerts-investigate``
* Decision-1 ``aggregate``/``routine``-> recorded in the decisions table (the summary)
* ``uncertain``, low confidence, an API error, or an invalid event -> ``alerts-review``

Queue messages are bounded: a message that would not fit the Storage Queue limit is written
in full to Blob Storage and the queue gets a small pointer to it (see ``enqueue_bounded``).
"""

from __future__ import annotations

import logging
import json
import os
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime
from typing import Any, Protocol
from xml.sax.saxutils import escape

from . import rules
from .decision import ChoiceAnswer, DecisionClient, DecisionError

logger = logging.getLogger("alert_router")

QUESTION = "route"
# Storage Queue accepts at most 64 KiB per message, measured on the encoded message the SDK
# sends inside an XML body. This app sends plain text (``messageEncoding: none`` in host.json),
# so the budget is applied to the XML-escaped UTF-8 text. 48 KiB also leaves room for the 4/3
# inflation if you switch the queues to base64 encoding (48 KiB * 4/3 = 64 KiB).
MAX_QUEUE_MESSAGE_BYTES = 48 * 1024
INSTRUCTIONS = (
    "You triage security and operations alerts for a security operations center. "
    "The state contains one or more related alerts for the same host or user, with asset context. "
    "Choose where the alerts should go."
)
ROUTE_OPTIONS = {
    "investigate": (
        "Possible active compromise or malicious activity that needs an investigation now: credential misuse, "
        "impossible travel, MFA fatigue, malware or ransomware behavior, command-and-control traffic, data "
        "exfiltration, privilege escalation, or suspicious activity on a privileged account or high-criticality asset."
    ),
    "aggregate": (
        "Low-risk security signal that was already blocked or contained, such as a quarantined phishing email or "
        "blocked malware download. Count it in the daily summary for trends; no individual investigation."
    ),
    "routine": (
        "Operational or hygiene event with no security impact, such as disk space, service health, certificate or "
        "password expiry, or a configuration notice. Record it for the operations summary."
    ),
    "uncertain": "The alerts do not contain enough information to choose, or their signals conflict.",
}


class AlertStore(Protocol):
    def claim_fingerprint(self, fingerprint: str, bucket: int, alert: dict[str, Any]) -> str: ...
    def record_decision(self, record: dict[str, Any]) -> None: ...
    def enqueue(self, queue_name: str, text: str) -> None: ...
    def save_payload(self, blob_name: str, text: str) -> None: ...


Decider = Callable[[dict[str, Any]], ChoiceAnswer]


def decision1_decider(client: DecisionClient) -> Decider:
    return lambda state: client.choose(state, QUESTION, INSTRUCTIONS, ROUTE_OPTIONS)


def _settings() -> dict[str, Any]:
    return {
        "investigate_queue": os.environ.get("INVESTIGATE_QUEUE", "alerts-investigate"),
        "review_queue": os.environ.get("REVIEW_QUEUE", "alerts-review"),
        "window_minutes": int(os.environ.get("DEDUPE_WINDOW_MINUTES", "60")),
        "min_confidence": float(os.environ.get("DECISION_MIN_CONFIDENCE", "0.5")),
        "max_concurrency": int(os.environ.get("DECISION_MAX_CONCURRENCY", "4")),
    }


def queue_message_size(text: str) -> int:
    """Bytes the message occupies in the Storage Queue request (XML-escaped UTF-8)."""
    return len(escape(text).encode("utf-8"))


def enqueue_bounded(store: AlertStore, queue_name: str, message: dict[str, Any]) -> str:
    """Enqueue ``message``; if it is too large for a queue message, offload it to Blob Storage.

    Returns the blob name when the payload was offloaded, otherwise ``""``. The pointer message
    keeps only small, bounded fields, so it always fits. The consumer loads the full payload
    from ``payload_blob``. The blob name is derived from the group ID, so a retried batch
    overwrites the same blob instead of creating a new one.
    """
    text = json.dumps(message, separators=(",", ":"))
    if queue_message_size(text) <= MAX_QUEUE_MESSAGE_BYTES:
        store.enqueue(queue_name, text)
        return ""
    blob_name = f"{queue_name}/{message['group_id']}.json"
    store.save_payload(blob_name, text)
    pointer = {
        "group_id": message["group_id"],
        "routed_at": message["routed_at"],
        "route": message["route"],
        "alert_count": message["alert_count"],
        "payload_blob": blob_name,
        "payload_bytes": len(text.encode("utf-8")),
    }
    store.enqueue(queue_name, json.dumps(pointer, separators=(",", ":")))
    return blob_name


def _route_group(group: list[dict[str, Any]], decider: Decider, min_confidence: float) -> dict[str, Any]:
    """Pick the route for one group. Never raises: any failure routes to review."""
    if rules.is_critical(group):
        return {"route": "investigate", "source": "critical_bypass", "reason": "Critical severity skips the model."}
    try:
        answer = decider(rules.build_state(group))
    except DecisionError as exc:
        return {"route": "review", "source": "decision1_error", "reason": str(exc)[:500]}
    except Exception as exc:  # noqa: BLE001 - fail safe: an unexpected error must not drop the alert.
        logger.exception("Decision-1 call failed")
        return {"route": "review", "source": "decision1_error", "reason": f"{type(exc).__name__}: {exc}"[:500]}

    decision = {
        "choice": answer.choice,
        "confidence": round(answer.confidence, 4),
        "probabilities": answer.probabilities,
    }
    if answer.choice == "uncertain":
        return {**decision, "route": "review", "source": "decision1", "reason": "Decision-1 chose uncertain."}
    if answer.probabilities and answer.confidence < min_confidence:
        return {
            **decision,
            "route": "review",
            "source": "decision1_low_confidence",
            "reason": f"Top probability {answer.confidence:.2f} is below {min_confidence:.2f}.",
        }
    return {**decision, "route": answer.choice, "source": "decision1", "reason": "Routed by Decision-1."}


def route_batch(
    bodies: list[bytes | str],
    *,
    store: AlertStore,
    decider: Decider,
    now: datetime | None = None,
    settings: dict[str, Any] | None = None,
) -> list[dict[str, Any]]:
    """Apply the rules and routing to one Event Hubs batch. Returns the decision records."""
    cfg = {**_settings(), **(settings or {})}
    now = now or datetime.now(UTC)
    routed_at = now.isoformat()
    records: list[dict[str, Any]] = []

    def finish(group_id: str, alerts: list[dict[str, Any]], outcome: dict[str, Any], payload: Any = None) -> None:
        lead = alerts[0] if alerts else {}
        record = {
            "group_id": group_id,
            "routed_at": routed_at,
            "alert_ids": [a["alert_id"] for a in alerts],
            "alert_count": len(alerts),
            "rule_id": lead.get("rule_id", ""),
            "title": lead.get("title", ""),
            "entity": rules.entity_of(lead) if alerts else "",
            "severity": rules.max_severity(alerts) if alerts else "",
            "choice": "",
            "confidence": 0.0,
            "probabilities": {},
            **outcome,
        }
        queue = {"investigate": cfg["investigate_queue"], "review": cfg["review_queue"]}.get(record["route"])
        record["destination"] = f"queue:{queue}" if queue else "summary"
        record["payload_blob"] = ""
        if queue:
            # Enqueue before recording, so a recorded decision always means the work was handed off.
            record["payload_blob"] = enqueue_bounded(store, queue, {
                "group_id": group_id,
                "routed_at": routed_at,
                "route": {k: record[k] for k in ("route", "source", "choice", "confidence", "probabilities", "reason")},
                "alert_count": len(alerts),
                "alerts": alerts,
                "assets": rules.asset_for(alerts[0]) if alerts else {},
                **({"raw_event": payload} if payload is not None else {}),
            })
        store.record_decision(record)
        records.append(record)

    survivors: list[dict[str, Any]] = []
    for index, body in enumerate(bodies):
        try:
            alert = rules.parse_alert(body, now)
        except rules.InvalidAlert as exc:
            raw = body.decode("utf-8", errors="replace") if isinstance(body, bytes | bytearray) else str(body)
            group_id = f"invalid-{now.strftime('%Y%m%dT%H%M%S')}-{index}"
            finish(group_id, [], {"route": "review", "source": "invalid_event", "reason": str(exc)}, raw)
            continue

        suppression = rules.match_suppression(alert)
        if suppression:
            finish(alert["alert_id"], [alert], {
                "route": "suppressed",
                "source": "suppression_rule",
                "reason": f"{suppression.get('id', '')}: {suppression.get('reason', '')}".strip(": "),
            })
            continue

        claim = store.claim_fingerprint(
            rules.fingerprint(alert), rules.window_bucket(alert, cfg["window_minutes"]), alert
        )
        if claim == "duplicate":
            continue
        survivors.append(alert)

    groups = rules.group_alerts(survivors)
    with ThreadPoolExecutor(max_workers=max(1, cfg["max_concurrency"])) as pool:
        outcomes = list(pool.map(lambda g: _route_group(g, decider, cfg["min_confidence"]), groups))
    for group, outcome in zip(groups, outcomes, strict=True):
        finish(rules.primary(group)["alert_id"], group, outcome)
    return records


def summarize(records: list[dict[str, Any]], received: int) -> str:
    counts: dict[str, int] = {}
    for record in records:
        counts[record["route"]] = counts.get(record["route"], 0) + 1
    routed = sum(r["alert_count"] or 1 for r in records)
    parts = " ".join(f"{k}={v}" for k, v in sorted(counts.items()))
    return f"received={received} groups={len(records)} deduplicated={received - routed} {parts}"
