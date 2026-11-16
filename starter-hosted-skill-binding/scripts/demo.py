# Copyright (c) Microsoft Corporation.
# Licensed under the MIT License.

"""Send support messages; e2e verifies completion logs for each submitted ticket.

Run from the template folder:
    python scripts/demo.py send [--sample T-1001] [--local]
    python scripts/demo.py e2e [--timeout 900]
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

TEMPLATE_DIR = Path(__file__).resolve().parent.parent
SAMPLES_DIR = TEMPLATE_DIR / "samples"
QUEUE_NAME = "support-requests"


def azd_values() -> dict[str, str]:
    out = subprocess.run(
        ["azd", "env", "get-values"], cwd=TEMPLATE_DIR, capture_output=True, text=True,
        check=True, shell=os.name == "nt",
    ).stdout
    values = {}
    for line in out.splitlines():
        key, sep, value = line.partition("=")
        if sep:
            values[key.strip()] = value.strip().strip('"')
    return values


def credential():
    from azure.identity import DefaultAzureCredential

    return DefaultAzureCredential(exclude_interactive_browser_credential=True)


def load_samples(only: str | None = None) -> list[dict[str, Any]]:
    samples = [json.loads(p.read_text(encoding="utf-8")) for p in sorted(SAMPLES_DIR.glob("*.json"))]
    samples = [s for s in samples if only is None or s["ticket_id"] == only]
    if not samples:
        raise SystemExit(f"No sample found for {only!r}")
    return samples


def send(messages: list[dict[str, Any]], local: bool) -> None:
    from azure.core.exceptions import ResourceExistsError
    from azure.storage.queue import QueueClient

    if local:
        queue = QueueClient.from_connection_string("UseDevelopmentStorage=true", QUEUE_NAME)
        try:
            queue.create_queue()
        except ResourceExistsError:
            pass
        with queue:
            _send_messages(queue, messages)
    else:
        account = azd_values()["AZURE_STORAGE_ACCOUNT_NAME"]
        with credential() as identity, QueueClient(
            f"https://{account}.queue.core.windows.net", QUEUE_NAME, credential=identity,
        ) as queue:
            _send_messages(queue, messages)


def _send_messages(queue: Any, messages: list[dict[str, Any]]) -> None:
    for message in messages:
        queue.send_message(json.dumps(message))
        print(f"sent {message['ticket_id']} -> queue '{QUEUE_NAME}'")


def completed_tickets(resource_id: str, since: datetime, ticket_ids: set[str]) -> set[str]:
    from azure.monitor.query import LogsQueryClient, LogsQueryStatus

    # IDs come from our sample files plus a UUID, and are encoded as Kusto literals.
    ids = ", ".join(json.dumps(ticket_id) for ticket_id in sorted(ticket_ids))
    query = (
        f"traces | where timestamp > datetime({since.isoformat()}) "
        '| where message startswith "Support triage completed: " '
        '| extend triage = parse_json(substring(message, strlen("Support triage completed: "))) '
        f"| where tostring(triage.ticket_id) in ({ids}) "
        "| distinct ticket_id = tostring(triage.ticket_id)"
    )
    with credential() as identity, LogsQueryClient(identity) as client:
        result = client.query_resource(resource_id, query, timespan=timedelta(days=1))
    if result.status != LogsQueryStatus.SUCCESS:
        return set()
    return {str(row[0]) for table in result.tables for row in table.rows}


def e2e(timeout: int) -> int:
    resource_id = azd_values()["APPLICATIONINSIGHTS_RESOURCE_ID"]
    run_id = uuid.uuid4().hex[:12]
    messages = [{**s, "ticket_id": f"{s['ticket_id']}-{run_id}"} for s in load_samples()]
    expected = {message["ticket_id"] for message in messages}
    started = datetime.now(timezone.utc) - timedelta(seconds=30)
    send(messages, local=False)
    print("Waiting for correlated completion logs; Application Insights ingestion can take minutes.")
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        completed = completed_tickets(resource_id, started, expected)
        missing = expected - completed
        if not missing:
            print(f"PASS: all {len(expected)} submitted tickets completed")
            return 0
        print(f"waiting for: {', '.join(sorted(missing))}")
        time.sleep(20)
    print("FAIL: not all submitted tickets produced a completion log", file=sys.stderr)
    return 1


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="command", required=True)
    send_parser = sub.add_parser("send")
    send_parser.add_argument("--local", action="store_true", help="Send to Azurite instead of Azure.")
    send_parser.add_argument("--sample", help="Send one ticket, for example T-1001.")
    e2e_parser = sub.add_parser("e2e")
    e2e_parser.add_argument("--timeout", type=int, default=900)
    args = parser.parse_args(argv)
    if args.command == "send":
        send(load_samples(args.sample), args.local)
        return 0
    return e2e(args.timeout)


if __name__ == "__main__":
    sys.exit(main())