"""Send sample records through the ingestion pipeline and see what happened to them.

Usage (from the template folder):

    python scripts/demo.py send                       # send every sample with fresh record IDs
    python scripts/demo.py send --sample 02-renamed-field
    python scripts/demo.py results                    # processed records, repair failures, DLQ count
    python scripts/demo.py e2e                        # send all samples and verify the outcome

Add ``--local`` to target ``func start`` (src/local.settings.json) instead of the azd environment.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
import time
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any

TEMPLATE_DIR = Path(__file__).resolve().parent.parent
SAMPLES_DIR = TEMPLATE_DIR / "samples"

# What should happen to each sample.
EXPECTED = {
    "01-valid": "processed",  # ingested directly, no model call
    "02-renamed-field": "repaired",  # recordId -> record_id, "21.5" -> 21.5
    "03-wrong-casing": "repaired",  # Source -> source, Value -> value
    "04-missing-value": "unrepairable",  # no measurement: nothing to repair without guessing
}


@dataclass
class Target:
    namespace: str
    queue_name: str
    processed_container: str
    failures_container: str
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
        namespace = values.get("ServiceBusConnection__fullyQualifiedNamespace", "")
        if not namespace or "<" in namespace:
            raise SystemExit("Set ServiceBusConnection__fullyQualifiedNamespace in src/local.settings.json first.")
        return Target(
            namespace=namespace,
            queue_name=values.get("INGEST_QUEUE", "records-local"),
            processed_container=values.get("PROCESSED_CONTAINER", "processed-data"),
            failures_container=values.get("FAILURES_CONTAINER", "repair-failures"),
            storage_connection=values.get("AzureWebJobsStorage", "UseDevelopmentStorage=true"),
        )
    values = _azd_values()
    return Target(
        namespace=values["SERVICEBUS_FULLY_QUALIFIED_NAMESPACE"],
        queue_name=values["INGEST_QUEUE"],
        processed_container=values["PROCESSED_CONTAINER"],
        failures_container=values["FAILURES_CONTAINER"],
        storage_account=values["AZURE_STORAGE_ACCOUNT_NAME"],
    )


def _credential():
    from azure.identity import DefaultAzureCredential

    return DefaultAzureCredential(exclude_interactive_browser_credential=True)


def _container(target: Target, name: str):
    from azure.core.exceptions import ResourceExistsError
    from azure.storage.blob import ContainerClient

    if target.storage_connection:
        container = ContainerClient.from_connection_string(target.storage_connection, name)
        try:
            container.create_container()
        except ResourceExistsError:
            pass
        return container
    return ContainerClient(f"https://{target.storage_account}.blob.core.windows.net", name, credential=_credential())


# ---------------------------------------------------------------------------- samples


def _is_id_field(name: str) -> bool:
    return re.sub(r"[^a-z0-9]", "", name.lower()) == "recordid"


def load_samples(only: str | None = None) -> dict[str, dict[str, Any]]:
    samples = {}
    for path in sorted(SAMPLES_DIR.glob("*.json")):
        if only is None or path.stem == only:
            samples[path.stem] = json.loads(path.read_text(encoding="utf-8"))
    if not samples:
        raise SystemExit(f"No sample named {only!r} in {SAMPLES_DIR}")
    return samples


def with_fresh_id(record: dict[str, Any], run_id: str) -> tuple[str, dict[str, Any]]:
    """Return ``(record_id, record)`` with the record's ID field (whatever its spelling) made unique."""
    record = dict(record)
    for name, value in record.items():
        if _is_id_field(name):
            record[name] = f"{value}-{run_id}"
            return record[name], record
    raise ValueError(f"sample has no record ID field: {record}")


# ---------------------------------------------------------------------------- Service Bus and Blob


def send(target: Target, messages: dict[str, dict[str, Any]]) -> None:
    """Send ``{message_id: record}``. The message ID is the record ID, as an upstream producer would set it."""
    from azure.servicebus import ServiceBusClient, ServiceBusMessage

    with ServiceBusClient(target.namespace, _credential()) as client:
        with client.get_queue_sender(target.queue_name) as sender:
            for message_id, record in messages.items():
                sender.send_messages(
                    ServiceBusMessage(json.dumps(record), message_id=message_id, content_type="application/json")
                )
                print(f"sent {message_id:<28} {json.dumps(record)}")
    print(f"-> Service Bus queue '{target.queue_name}' on {target.namespace}")


def dead_letter_count(target: Target) -> int:
    from azure.servicebus import ServiceBusClient, ServiceBusSubQueue

    count, sequence = 0, 0
    with ServiceBusClient(target.namespace, _credential()) as client:
        with client.get_queue_receiver(target.queue_name, sub_queue=ServiceBusSubQueue.DEAD_LETTER) as receiver:
            while True:
                batch = receiver.peek_messages(max_message_count=100, sequence_number=sequence)
                if not batch:
                    return count
                count += len(batch)
                sequence = batch[-1].sequence_number + 1


def read_processed(target: Target, record_ids: list[str] | None = None) -> dict[str, dict[str, Any]]:
    """Return ``{record_id: document}`` from ``<source>/<yyyy/mm/dd>/<record_id>.json`` blobs."""
    container = _container(target, target.processed_container)
    wanted = set(record_ids or [])
    results = {}
    for blob in container.list_blobs():
        record_id = blob.name.rsplit("/", 1)[-1].removesuffix(".json")
        if not wanted or record_id in wanted:
            doc = json.loads(container.download_blob(blob.name).readall())
            results[record_id] = {**doc, "_blob": blob.name, "_modified": blob.last_modified.isoformat()}
    return results


def read_failures(target: Target, message_ids: list[str] | None = None) -> dict[str, dict[str, Any]]:
    from azure.core.exceptions import ResourceNotFoundError

    container = _container(target, target.failures_container)
    results = {}
    if message_ids:
        for message_id in message_ids:
            try:
                results[message_id] = json.loads(container.download_blob(f"{message_id}.json").readall())
            except ResourceNotFoundError:
                continue
    else:
        for blob in container.list_blobs():
            doc = json.loads(container.download_blob(blob.name).readall())
            results[blob.name.removesuffix(".json")] = {**doc, "_modified": blob.last_modified.isoformat()}
    return results


# ---------------------------------------------------------------------------- results


def _latest(docs: dict[str, dict[str, Any]], limit: int) -> list[tuple[str, dict[str, Any]]]:
    return sorted(docs.items(), key=lambda kv: kv[1].get("_modified", ""), reverse=True)[:limit]


def print_results(processed: dict[str, dict[str, Any]], failures: dict[str, dict[str, Any]], limit: int = 20) -> None:
    print(f"processed-data ({len(processed)}):")
    for record_id, doc in _latest(processed, limit):
        flag = f"REPAIRED ({doc.get('repair_changes', '')})" if doc.get("repaired") else "ingested"
        print(f"  {record_id:<28} source={doc.get('source')!s:<14} value={doc.get('value')!s:<6} {flag}")
    print(f"repair-failures ({len(failures)}):")
    for message_id, doc in _latest(failures, limit):
        print(f"  {message_id:<28} {doc.get('reason')}")


def validate(
    expected: dict[str, str], processed: dict[str, dict[str, Any]], failures: dict[str, dict[str, Any]]
) -> list[str]:
    """Check ``{record_id: outcome}`` against what landed in processed-data and repair-failures."""
    errors = []
    for record_id, outcome in expected.items():
        doc = processed.get(record_id)
        if outcome == "unrepairable":
            if doc is not None:
                errors.append(f"{record_id}: expected unrepairable but it was ingested")
            elif record_id not in failures:
                errors.append(f"{record_id}: not in repair-failures yet")
            elif not failures[record_id].get("reason"):
                errors.append(f"{record_id}: repair failure has no reason")
            continue
        if doc is None:
            errors.append(f"{record_id}: not in processed-data yet")
            continue
        if bool(doc.get("repaired")) != (outcome == "repaired"):
            errors.append(f"{record_id}: expected repaired={outcome == 'repaired'}, got {doc.get('repaired')!r}")
        if not isinstance(doc.get("value"), int | float) or not re.fullmatch(r"[a-z0-9-]+", str(doc.get("source"))):
            errors.append(f"{record_id}: output was not normalized: {doc}")
    return errors


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="command", required=True)
    for name in ("send", "results", "e2e"):
        p = sub.add_parser(name)
        p.add_argument("--local", action="store_true", help="Use src/local.settings.json (func start).")
        if name == "send":
            p.add_argument("--sample", help="Send one sample by file name, for example 02-renamed-field.")
        if name == "e2e":
            p.add_argument("--timeout", type=int, default=600)
    args = parser.parse_args(argv)

    target = resolve_target(args.local)

    if args.command == "results":
        print_results(read_processed(target), read_failures(target))
        print(f"dead-letter queue '{target.queue_name}/$DeadLetterQueue': {dead_letter_count(target)} message(s)")
        return 0

    run_id = uuid.uuid4().hex[:6]
    samples = load_samples(getattr(args, "sample", None))
    messages, expected = {}, {}
    for name, record in samples.items():
        record_id, fresh = with_fresh_id(record, run_id)
        messages[record_id] = fresh
        expected[record_id] = EXPECTED.get(name, "processed")
    send(target, messages)
    if args.command == "send":
        print("Repairs take a minute or two (3 failed deliveries, then the hosted skill). Then run: demo.py results")
        return 0

    ids = list(expected)
    deadline = time.monotonic() + args.timeout
    while True:
        processed, failures = read_processed(target, ids), read_failures(target, ids)
        errors = validate(expected, processed, failures)
        if not errors or time.monotonic() >= deadline:
            break
        print(f"waiting... {len(expected) - len(errors)}/{len(expected)} records settled")
        time.sleep(10)

    print_results(processed, failures)
    for error in errors:
        print(f"FAIL {error}", file=sys.stderr)
    print("e2e passed" if not errors else "e2e failed")
    return 1 if errors else 0


if __name__ == "__main__":
    sys.exit(main())
