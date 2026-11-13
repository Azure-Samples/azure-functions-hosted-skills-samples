# Copyright (c) Microsoft Corporation.
# Licensed under the MIT License.

"""Send a burst of sample alerts to Event Hubs and check how they were handled.

Usage (from the template folder):

    python scripts/demo.py send                 # send the sample burst to the deployed Event Hub
    python scripts/demo.py results              # print today's routing decisions and investigations
    python scripts/demo.py e2e                  # send a unique copy of the burst and verify the outcome

Add ``--local`` to read storage from Azurite and the Event Hubs namespace from
``src/local.settings.json`` (for ``func start`` pointed at the deployed namespace).
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time
import uuid
from collections import defaultdict
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

TEMPLATE_DIR = Path(__file__).resolve().parent.parent
SAMPLES_FILE = TEMPLATE_DIR / "samples" / "alert-burst.json"
AZURITE = "UseDevelopmentStorage=true"

CRITICAL = "ALR-1001"
SUSPICIOUS = "ALR-1002"
DUPLICATES = ["ALR-1003", "ALR-1004", "ALR-1005", "ALR-1006", "ALR-1007"]
ROUTINE = "ALR-1008"
SUPPRESSED = "ALR-1009"
BLOCKED_PHISH = "ALR-1010"
DECISION_SOURCES = {"decision1", "decision1_low_confidence"}

# What a reviewer would expect from Decision-1. A mismatch is reported but does not fail the
# run, because the model's judgment can reasonably differ on borderline alerts.
EXPECTED_ROUTE = {
    SUSPICIOUS: {"investigate"},
    DUPLICATES[0]: {"routine"},
    ROUTINE: {"routine"},
    BLOCKED_PHISH: {"aggregate"},
}


@dataclass
class Target:
    eventhub_namespace: str
    eventhub_name: str
    decisions_table: str
    fingerprints_table: str
    investigations_container: str
    storage_connection: str = ""
    storage_account: str = ""


def _azd_values() -> dict[str, str]:
    out = subprocess.run(
        ["azd", "env", "get-values"], cwd=TEMPLATE_DIR, capture_output=True, text=True, check=True,
        shell=os.name == "nt",
    ).stdout
    values = {}
    for line in out.splitlines():
        key, sep, value = line.partition("=")
        if sep:
            values[key.strip()] = value.strip().strip('"')
    return values


def _local_values() -> dict[str, str]:
    path = TEMPLATE_DIR / "src" / "local.settings.json"
    if not path.exists():
        path = TEMPLATE_DIR / "src" / "local.settings.json.sample"
    return json.loads(path.read_text(encoding="utf-8"))["Values"]


def resolve_target(local: bool) -> Target:
    if local:
        values = _local_values()
        return Target(
            eventhub_namespace=values.get("EVENTHUB__fullyQualifiedNamespace", ""),
            eventhub_name=values.get("EVENTHUB_NAME", "alerts"),
            decisions_table=values.get("DECISIONS_TABLE", "alertdecisions"),
            fingerprints_table=values.get("FINGERPRINTS_TABLE", "alertfingerprints"),
            investigations_container=values.get("INVESTIGATIONS_CONTAINER", "investigations"),
            storage_connection=values.get("AzureWebJobsStorage", AZURITE),
        )
    values = _azd_values()
    return Target(
        eventhub_namespace=values["EVENTHUB_FULLY_QUALIFIED_NAMESPACE"],
        eventhub_name=values["EVENTHUB_NAME"],
        decisions_table=values["DECISIONS_TABLE"],
        fingerprints_table=values["FINGERPRINTS_TABLE"],
        investigations_container=values["INVESTIGATIONS_CONTAINER"],
        storage_account=values["AZURE_STORAGE_ACCOUNT_NAME"],
    )


def _credential():
    from azure.identity import DefaultAzureCredential

    return DefaultAzureCredential(exclude_interactive_browser_credential=True)


def _table(target: Target, name: str):
    from azure.data.tables import TableClient

    if target.storage_connection:
        return TableClient.from_connection_string(target.storage_connection, name)
    return TableClient(f"https://{target.storage_account}.table.core.windows.net", name, credential=_credential())


def _container(target: Target):
    from azure.storage.blob import ContainerClient

    if target.storage_connection:
        return ContainerClient.from_connection_string(target.storage_connection, target.investigations_container)
    return ContainerClient(
        f"https://{target.storage_account}.blob.core.windows.net", target.investigations_container,
        credential=_credential(),
    )


def load_samples() -> list[dict[str, Any]]:
    return json.loads(SAMPLES_FILE.read_text(encoding="utf-8"))


def prepare_run(samples: list[dict[str, Any]], run_id: str, now: datetime) -> list[dict[str, Any]]:
    """Give each alert a unique ID and a run-tagged source so a rerun isn't deduplicated as a repeat."""
    detected_at = now.replace(microsecond=0).isoformat()
    return [
        {**s, "alert_id": f"{s['alert_id']}-{run_id}", "source": f"{s['source']}#{run_id}", "detected_at": detected_at}
        for s in samples
    ]


def send(target: Target, alerts: list[dict[str, Any]]) -> None:
    from azure.eventhub import EventData, EventHubProducerClient

    if not target.eventhub_namespace or target.eventhub_namespace.startswith("<"):
        raise SystemExit("Event Hubs namespace is not configured. Run azd up, or set it in src/local.settings.json.")
    by_key: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for alert in alerts:
        # Same entity -> same partition, so related alerts and duplicates keep their order.
        by_key[(alert.get("host") or alert.get("user") or "unknown").lower()].append(alert)

    producer = EventHubProducerClient(target.eventhub_namespace, target.eventhub_name, _credential())
    with producer:
        for key, group in by_key.items():
            batch = producer.create_batch(partition_key=key)
            for alert in group:
                event = EventData(json.dumps(alert))
                event.content_type = "application/json"
                batch.add(event)
            producer.send_batch(batch)
    for alert in alerts:
        print(f"sent {alert['alert_id']} ({alert['severity']}, {alert['rule_id']}) -> event hub '{target.eventhub_name}'")


def read_decisions(target: Target, since: datetime) -> list[dict[str, Any]]:
    from azure.core.exceptions import ResourceNotFoundError

    table = _table(target, target.decisions_table)
    start = (since - timedelta(days=1)).date().isoformat()
    try:
        entities = list(table.query_entities(f"PartitionKey ge '{start}'"))
    except ResourceNotFoundError:
        return []
    records = []
    for entity in entities:
        record = dict(entity)
        for key in ("alert_ids", "probabilities"):
            try:
                record[key] = json.loads(record.get(key) or "null")
            except json.JSONDecodeError:
                record[key] = None
        records.append(record)
    return records


def read_duplicate_count(target: Target, alert_id: str) -> int | None:
    from azure.core.exceptions import ResourceNotFoundError

    table = _table(target, target.fingerprints_table)
    try:
        rows = list(table.query_entities("FirstAlertId eq @id", parameters={"id": alert_id}))
    except ResourceNotFoundError:
        return None
    return int(rows[0].get("DuplicateCount", 0)) if rows else None


def read_reports(target: Target, group_ids: list[str]) -> dict[str, dict[str, Any]]:
    from azure.core.exceptions import ResourceNotFoundError

    container = _container(target)
    reports = {}
    for group_id in group_ids:
        try:
            reports[group_id] = json.loads(container.download_blob(f"{group_id}.json").readall())
        except ResourceNotFoundError:
            continue
    return reports


def collect(target: Target, ids: dict[str, str], since: datetime) -> dict[str, Any]:
    """Everything the e2e check needs: decision records per sample, dedupe counts, and reports."""
    wanted = set(ids.values())
    by_alert: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for record in read_decisions(target, since):
        for alert_id in record.get("alert_ids") or []:
            if alert_id in wanted:
                by_alert[alert_id].append(record)
    investigate = sorted({r["group_id"] for rs in by_alert.values() for r in rs if r.get("route") == "investigate"})
    return {
        "records": {base: by_alert.get(full, []) for base, full in ids.items()},
        "duplicate_count": read_duplicate_count(target, ids[DUPLICATES[0]]),
        "reports": read_reports(target, investigate),
        "investigate_groups": investigate,
    }


def is_complete(state: dict[str, Any]) -> bool:
    records = state["records"]
    handled = [CRITICAL, SUSPICIOUS, ROUTINE, SUPPRESSED, BLOCKED_PHISH, DUPLICATES[0]]
    if any(not records.get(base) for base in handled):
        return False
    if (state["duplicate_count"] or 0) < len(DUPLICATES) - 1:
        return False
    return all(group in state["reports"] for group in state["investigate_groups"])


def _only(records: list[dict[str, Any]]) -> dict[str, Any]:
    return records[0] if len(records) == 1 else {}


def validate(state: dict[str, Any]) -> tuple[list[str], list[str]]:
    errors: list[str] = []
    notes: list[str] = []
    records = state["records"]

    critical = _only(records.get(CRITICAL, []))
    if not critical:
        errors.append(f"{CRITICAL}: expected exactly one decision record, found {len(records.get(CRITICAL, []))}")
    elif critical.get("route") != "investigate" or critical.get("source") != "critical_bypass":
        errors.append(f"{CRITICAL}: critical alert routed {critical.get('route')}/{critical.get('source')}")
    else:
        report = state["reports"].get(critical["group_id"])
        if report is None:
            errors.append(f"{CRITICAL}: no investigation report")
        else:
            if report.get("verdict") not in {"malicious", "suspicious", "benign", "inconclusive"}:
                errors.append(f"{CRITICAL}: unexpected verdict {report.get('verdict')!r}")
            if not report.get("evidence"):
                errors.append(f"{CRITICAL}: report has no evidence")
            if not report.get("recommended_containment"):
                errors.append(f"{CRITICAL}: report has no recommended containment")
            if any(a.get("executed") is not False for a in report.get("recommended_containment", [])):
                errors.append(f"{CRITICAL}: a containment action is not marked executed=false")
            if not report.get("requires_human_approval"):
                errors.append(f"{CRITICAL}: report does not require human approval")

    first_dupe = records.get(DUPLICATES[0], [])
    if len(first_dupe) != 1:
        errors.append(f"{DUPLICATES[0]}: expected one decision record for the duplicate set, found {len(first_dupe)}")
    for dupe in DUPLICATES[1:]:
        if records.get(dupe):
            errors.append(f"{dupe}: duplicate alert was routed instead of deduplicated")
    if (state["duplicate_count"] or 0) < len(DUPLICATES) - 1:
        errors.append(f"{DUPLICATES[0]}: duplicate count {state['duplicate_count']}, expected {len(DUPLICATES) - 1}")

    suppressed = _only(records.get(SUPPRESSED, []))
    if suppressed.get("route") != "suppressed":
        errors.append(f"{SUPPRESSED}: known-benign scan was not suppressed ({suppressed.get('route')})")

    routine = _only(records.get(ROUTINE, []))
    if routine.get("source") not in DECISION_SOURCES:
        errors.append(f"{ROUTINE}: not routed by Decision-1 (source {routine.get('source')!r})")

    decided = [r for rs in records.values() for r in rs if r.get("source") in DECISION_SOURCES]
    if not any(r.get("probabilities") for r in decided):
        errors.append("no Decision-1 record has option probabilities")
    summarized = [r for rs in records.values() for r in rs if r.get("route") in {"aggregate", "routine"}]
    if not summarized:
        errors.append("no low-risk alert was summarized (routed aggregate or routine)")

    for group_id in state["investigate_groups"]:
        if group_id not in state["reports"]:
            errors.append(f"{group_id}: routed to investigate but no report was written")

    for base, expected in EXPECTED_ROUTE.items():
        record = _only(records.get(base, []))
        if record and record.get("route") not in expected:
            notes.append(f"{base}: routed {record.get('route')!r} ({record.get('source')}); a reviewer would expect {sorted(expected)}")
    return errors, notes


def print_state(state: dict[str, Any]) -> None:
    print("\nRouting decisions:")
    for base, recs in state["records"].items():
        for r in recs:
            probs = r.get("probabilities") or {}
            top = ", ".join(f"{k}={v:.2f}" for k, v in sorted(probs.items(), key=lambda kv: -kv[1])) if probs else "-"
            print(f"  {base}: {str(r.get('route')):<11} source={str(r.get('source')):<24} alerts={r.get('alert_count')} [{top}]")
        if not recs:
            print(f"  {base}: (no record — deduplicated or not processed yet)")
    print(f"\nDuplicate count on {DUPLICATES[0]}: {state['duplicate_count']}")
    for group_id, report in state["reports"].items():
        print(f"\nInvestigation {group_id}: {report.get('verdict')} ({report.get('confidence')}) severity={report.get('severity')}")
        print(f"  {report.get('summary')}")
        for action in report.get("recommended_containment", []):
            print(f"  - recommend {action['action']} {action['target']} [{action['urgency']}] executed={action['executed']}")


def results(target: Target) -> None:
    now = datetime.now(UTC)
    decisions = read_decisions(target, now)
    if not decisions:
        print("No routing decisions recorded yet.")
        return
    counts: dict[str, int] = defaultdict(int)
    for record in sorted(decisions, key=lambda r: r.get("routed_at", "")):
        counts[record.get("route", "?")] += 1
        print(f"{record.get('routed_at', '')[:19]} {str(record.get('group_id')):<24} {str(record.get('route')):<11} "
              f"{str(record.get('source')):<24} {record.get('title', '')}")
    print("\n" + ", ".join(f"{k}={v}" for k, v in sorted(counts.items())))
    reports = read_reports(target, [r["group_id"] for r in decisions if r.get("route") == "investigate"])
    for group_id, report in reports.items():
        print(f"\n{group_id}: {report.get('verdict')} — {report.get('summary')}")


def main(argv: list[str] | None = None) -> int:
    # Model text can contain characters the Windows console code page can't print.
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(errors="replace")
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="command", required=True)
    for name in ("send", "results", "e2e"):
        p = sub.add_parser(name)
        p.add_argument("--local", action="store_true", help="Use Azurite / src/local.settings.json.")
        if name == "e2e":
            p.add_argument("--timeout", type=int, default=900)
    args = parser.parse_args(argv)

    target = resolve_target(args.local)
    now = datetime.now(UTC)

    if args.command == "send":
        send(target, prepare_run(load_samples(), uuid.uuid4().hex[:6], now))
        return 0
    if args.command == "results":
        results(target)
        return 0

    run_id = uuid.uuid4().hex[:6]
    alerts = prepare_run(load_samples(), run_id, now)
    ids = {f"ALR-{a['alert_id'].split('-')[1]}": a["alert_id"] for a in alerts}
    send(target, alerts)

    deadline = time.monotonic() + args.timeout
    while True:
        state = collect(target, ids, now)
        if is_complete(state) or time.monotonic() >= deadline:
            break
        done = sum(1 for recs in state["records"].values() if recs)
        print(f"waiting... {done} alerts routed, {len(state['reports'])}/{len(state['investigate_groups'])} investigations written")
        time.sleep(15)

    print_state(state)
    errors, notes = validate(state)
    for note in notes:
        print(f"note: {note}")
    for error in errors:
        print(f"FAIL {error}", file=sys.stderr)
    print("e2e passed" if not errors else "e2e failed")
    return 1 if errors else 0


if __name__ == "__main__":
    sys.exit(main())
