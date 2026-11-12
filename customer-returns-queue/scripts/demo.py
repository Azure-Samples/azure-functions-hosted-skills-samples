"""Send sample return requests and read the hosted skill's recommendations.

Usage (from the template folder):

    python scripts/demo.py send                 # send all samples to the deployed app
    python scripts/demo.py send --sample RR-1001
    python scripts/demo.py results --wait 300   # wait for and print recommendations
    python scripts/demo.py e2e                  # send unique copies and verify results

Add ``--local`` to target ``func start`` with Azurite instead of the azd environment.
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any

TEMPLATE_DIR = Path(__file__).resolve().parent.parent
SAMPLES_DIR = TEMPLATE_DIR / "samples"
AZURITE = "UseDevelopmentStorage=true"
DECISIONS = {"approve_standard", "approve_exception", "decline", "escalate"}

# What a reviewer would expect for each sample. A mismatch is reported but does not fail
# the run, because the model's judgment can reasonably differ on borderline cases.
EXPECTED = {
    "RR-1001": {"approve_exception"},
    "RR-1002": {"approve_standard"},
    "RR-1003": {"decline", "escalate"},
}


@dataclass
class Target:
    provider: str  # storage | servicebus
    queue_name: str
    container: str
    storage_connection: str = ""
    storage_account: str = ""
    servicebus_namespace: str = ""


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
        provider = "servicebus" if values.get("RETURNS_TRIGGER_TYPE") == "service_bus_queue_trigger" else "storage"
        return Target(
            provider=provider,
            queue_name="customer-returns",
            container=values.get("RECOMMENDATIONS_CONTAINER", "return-recommendations"),
            storage_connection=values.get("AzureWebJobsStorage", AZURITE),
            servicebus_namespace=values.get("RETURNS_QUEUE_CONNECTION__fullyQualifiedNamespace", ""),
        )
    values = _azd_values()
    return Target(
        provider=values.get("QUEUE_PROVIDER", "storage"),
        queue_name=values["RETURNS_QUEUE_NAME"],
        container=values["RECOMMENDATIONS_CONTAINER"],
        storage_account=values["AZURE_STORAGE_ACCOUNT_NAME"],
        servicebus_namespace=values.get("SERVICEBUS_FULLY_QUALIFIED_NAMESPACE", ""),
    )


def _credential():
    from azure.identity import DefaultAzureCredential

    return DefaultAzureCredential(exclude_interactive_browser_credential=True)


def _container_client(target: Target):
    from azure.storage.blob import ContainerClient

    if target.storage_connection:
        return ContainerClient.from_connection_string(target.storage_connection, target.container)
    return ContainerClient(
        f"https://{target.storage_account}.blob.core.windows.net", target.container, credential=_credential()
    )


def load_samples(only: str | None = None) -> list[dict[str, Any]]:
    samples = []
    for path in sorted(SAMPLES_DIR.glob("*.json")):
        sample = json.loads(path.read_text(encoding="utf-8"))
        if only is None or sample["request_id"] == only:
            samples.append(sample)
    if not samples:
        raise SystemExit(f"No sample found for {only!r} in {SAMPLES_DIR}")
    return samples


def send(target: Target, messages: list[dict[str, Any]]) -> None:
    bodies = [json.dumps(m) for m in messages]
    if target.provider == "servicebus":
        from azure.servicebus import ServiceBusClient, ServiceBusMessage

        if not target.servicebus_namespace:
            raise SystemExit("Service Bus namespace is not configured.")
        with ServiceBusClient(target.servicebus_namespace, _credential()) as client:
            with client.get_queue_sender(target.queue_name) as sender:
                for message, body in zip(messages, bodies):
                    sender.send_messages(
                        ServiceBusMessage(body, message_id=message["request_id"], content_type="application/json")
                    )
    else:
        from azure.core.exceptions import ResourceExistsError
        from azure.storage.queue import QueueClient

        if target.storage_connection:
            queue = QueueClient.from_connection_string(target.storage_connection, target.queue_name)
            try:
                queue.create_queue()
            except ResourceExistsError:
                pass
        else:
            queue = QueueClient(
                f"https://{target.storage_account}.queue.core.windows.net", target.queue_name, credential=_credential()
            )
        for body in bodies:
            queue.send_message(body)

    for message in messages:
        print(f"sent {message['request_id']} -> {target.provider} queue '{target.queue_name}'")


def read_results(target: Target, request_ids: list[str] | None = None) -> dict[str, dict[str, Any]]:
    from azure.core.exceptions import ResourceNotFoundError

    container = _container_client(target)
    results: dict[str, dict[str, Any]] = {}
    try:
        if request_ids:
            for request_id in request_ids:
                try:
                    data = container.download_blob(f"{request_id}.json").readall()
                except ResourceNotFoundError:
                    continue
                results[request_id] = json.loads(data)
        else:
            for blob in container.list_blobs():
                results[blob.name.removesuffix(".json")] = json.loads(container.download_blob(blob.name).readall())
    except ResourceNotFoundError:
        pass
    return results


def wait_for(target: Target, request_ids: list[str], timeout: int) -> dict[str, dict[str, Any]]:
    deadline = time.monotonic() + timeout
    results: dict[str, dict[str, Any]] = {}
    while True:
        results = read_results(target, request_ids)
        if len(results) == len(request_ids) or time.monotonic() >= deadline:
            return results
        print(f"waiting... {len(results)}/{len(request_ids)} recommendations recorded")
        time.sleep(10)


def print_results(results: dict[str, dict[str, Any]]) -> None:
    if not results:
        print("No recommendations recorded yet.")
        return
    for request_id, doc in sorted(results.items()):
        approval = "needs approval" if doc.get("requires_human_approval") else "auto"
        print(f"\n{request_id}: {doc.get('decision')} / {doc.get('resolution')} ({approval})")
        for reason in doc.get("reasons", []):
            print(f"  - {reason}")


def _base_id(request_id: str) -> str:
    return "-".join(request_id.split("-")[:2])


def validate(results: dict[str, dict[str, Any]], expected_ids: list[str]) -> list[str]:
    errors = []
    for request_id in expected_ids:
        doc = results.get(request_id)
        if doc is None:
            errors.append(f"{request_id}: no recommendation recorded")
            continue
        if doc.get("decision") not in DECISIONS:
            errors.append(f"{request_id}: unexpected decision {doc.get('decision')!r}")
        if not doc.get("reasons"):
            errors.append(f"{request_id}: no reasons")
        if doc.get("decision") != "approve_standard" and not doc.get("requires_human_approval"):
            errors.append(f"{request_id}: non-standard decision without human approval")
        hint = EXPECTED.get(_base_id(request_id))
        if hint and doc.get("decision") not in hint:
            print(f"note: {request_id} decision {doc.get('decision')!r}; a reviewer would expect {sorted(hint)}")
    return errors


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="command", required=True)
    for name in ("send", "results", "e2e"):
        p = sub.add_parser(name)
        p.add_argument("--local", action="store_true", help="Use Azurite / src/local.settings.json.")
        if name == "send":
            p.add_argument("--sample", help="Send one sample by request_id, for example RR-1001.")
        if name == "results":
            p.add_argument("--wait", type=int, default=0, help="Seconds to wait for all samples.")
        if name == "e2e":
            p.add_argument("--timeout", type=int, default=600)
    args = parser.parse_args(argv)

    target = resolve_target(args.local)

    if args.command == "send":
        send(target, load_samples(args.sample))
        return 0

    if args.command == "results":
        if args.wait:
            ids = [s["request_id"] for s in load_samples()]
            print_results(wait_for(target, ids, args.wait))
        else:
            print_results(read_results(target))
        return 0

    run_id = uuid.uuid4().hex[:6]
    messages = [{**s, "request_id": f"{s['request_id']}-{run_id}"} for s in load_samples()]
    ids = [m["request_id"] for m in messages]
    send(target, messages)
    results = wait_for(target, ids, args.timeout)
    print_results(results)
    errors = validate(results, ids)
    for error in errors:
        print(f"FAIL {error}", file=sys.stderr)
    print("e2e passed" if not errors else "e2e failed")
    return 1 if errors else 0


if __name__ == "__main__":
    sys.exit(main())
