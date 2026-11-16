# Copyright (c) Microsoft Corporation.
# Licensed under the MIT License.

"""Send supplier changes and verify recommendation blobs.

From the sample folder: python scripts/demo.py send|results|e2e [--local].
Local means Azurite for blobs, but REAL Azure Service Bus for messages.
E2e verifies blobs only, not buyer-notification delivery.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import subprocess
import sys
import time
import uuid
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import Any

TEMPLATE_DIR = Path(__file__).resolve().parent.parent
SAMPLES_DIR = TEMPLATE_DIR / "samples"
AZURITE = "UseDevelopmentStorage=true"
EXPECTED = {
    "SC-1001": ("accept_update", False, []),
    "SC-1002": (
        "buyer_review", True,
        ["A confirmed delivery date is missing.", "Partial delivery leaves 120 units outstanding."],
    ),
    "SC-1003": (
        "buyer_review", True,
        ["Substitute part is not on the purchase order's approved list."],
    ),
}


@dataclass
class Target:
    queue_name: str = "supplier-changes"
    review_queue_name: str = "supplier-reviews"
    container: str = "supplier-change-recommendations"
    storage_connection: str = ""
    storage_account: str = ""
    servicebus_namespace: str = ""
    review_namespace: str = ""
    local: bool = False


def _azd_values() -> dict[str, str]:
    out = subprocess.run(
        ["azd", "env", "get-values"], cwd=TEMPLATE_DIR, capture_output=True,
        text=True, check=True, shell=os.name == "nt",
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
    # Do not validate namespaces here: inspecting local blobs needs no Service Bus.
    if local:
        values = _local_values()
        return Target(
            local=True,
            container=values.get("RECOMMENDATIONS_CONTAINER", "supplier-change-recommendations"),
            storage_connection=values.get("AzureWebJobsStorage", AZURITE),
            servicebus_namespace=values.get("SERVICEBUS__fullyQualifiedNamespace", ""),
            review_namespace=values.get("SUPPLIER_SERVICEBUS_NAMESPACE", ""),
        )
    values = _azd_values()
    return Target(
        queue_name=values["SERVICEBUS_QUEUE_NAME"],
        review_queue_name=values["SERVICEBUS_REVIEW_QUEUE_NAME"],
        container=values["RECOMMENDATIONS_CONTAINER"],
        storage_account=values["AZURE_STORAGE_ACCOUNT_NAME"],
        servicebus_namespace=values["SERVICEBUS_FULLY_QUALIFIED_NAMESPACE"],
        review_namespace=values["SERVICEBUS_FULLY_QUALIFIED_NAMESPACE"],
    )


def _namespace(value: str) -> str:
    value = value.strip().lower()
    if (
        re.fullmatch(r"[a-z][a-z0-9-]{4,48}[a-z0-9]\.servicebus\.windows\.net", value) is None
        or any(token in value for token in ("your-", "example", "placeholder", "changeme", "replace", "<", ">"))
    ):
        raise ValueError("Configure a real Service Bus namespace (name.servicebus.windows.net); Azurite is blob-only.")
    return value


def validate_send_target(target: Target) -> str:
    namespace = _namespace(target.servicebus_namespace)
    review = _namespace(target.review_namespace)
    if namespace != review:
        raise ValueError("Input SERVICEBUS namespace and output SUPPLIER_SERVICEBUS_NAMESPACE must match.")
    if target.queue_name != "supplier-changes" or target.review_queue_name != "supplier-reviews":
        raise ValueError("The application uses supplier-changes and supplier-reviews; check the azd environment.")
    return namespace


def _credential(local: bool):
    if local:
        from azure.identity import AzureCliCredential

        return AzureCliCredential()
    from azure.identity import DefaultAzureCredential

    return DefaultAzureCredential(
        managed_identity_client_id=os.environ.get("AZURE_CLIENT_ID"),
        exclude_interactive_browser_credential=True,
    )


@contextmanager
def _container_client(target: Target):
    from azure.storage.blob import ContainerClient

    if target.storage_connection:
        with ContainerClient.from_connection_string(target.storage_connection, target.container) as container:
            yield container
    else:
        if not target.storage_account:
            raise ValueError("Configure blob storage (AzureWebJobsStorage or AZURE_STORAGE_ACCOUNT_NAME).")
        with _credential(target.local) as credential:
            with ContainerClient(
                f"https://{target.storage_account}.blob.core.windows.net",
                target.container, credential=credential,
            ) as container:
                yield container


def load_samples(only: str | None = None) -> list[dict[str, Any]]:
    samples = []
    for path in sorted(SAMPLES_DIR.glob("*.json")):
        sample = json.loads(path.read_text(encoding="utf-8"))
        if only is None or sample["change_id"] == only:
            samples.append(sample)
    if not samples:
        raise ValueError(f"No sample found for {only!r} in {SAMPLES_DIR}")
    return samples


def unique_samples() -> list[dict[str, Any]]:
    run_id = uuid.uuid4().hex[:12]
    return [{**sample, "change_id": f"{sample['change_id']}-{run_id}"} for sample in load_samples()]


def send(target: Target, messages: list[dict[str, Any]]) -> None:
    namespace = validate_send_target(target)
    # Import SDKs only after validation; pure demo validation needs no Azure packages.
    from azure.servicebus import ServiceBusClient, ServiceBusMessage

    with _credential(target.local) as credential:
        with ServiceBusClient(namespace, credential) as client:
            with client.get_queue_sender(target.queue_name) as sender:
                for message in messages:
                    sender.send_messages(ServiceBusMessage(
                        json.dumps(message), message_id=message["change_id"], content_type="application/json",
                    ))
                    print(f"sent {message['change_id']} -> Service Bus queue '{target.queue_name}'")


def read_results(target: Target, change_ids: list[str]) -> dict[str, dict[str, Any]]:
    from azure.core.exceptions import ResourceNotFoundError

    results = {}
    with _container_client(target) as container:
        # Never list unrelated blobs: a previous run must not satisfy this run.
        for change_id in change_ids:
            try:
                data = container.download_blob(f"{change_id}.json").readall()
            except ResourceNotFoundError:
                continue
            results[change_id] = json.loads(data)
    return results


def wait_for(target: Target, change_ids: list[str], timeout: int) -> dict[str, dict[str, Any]]:
    deadline = time.monotonic() + timeout
    while True:
        results = read_results(target, change_ids)
        if all(change_id in results for change_id in change_ids):
            return results
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return results
        print(f"waiting... {len(results)}/{len(change_ids)} recommendations recorded")
        time.sleep(min(5, remaining))


def print_results(results: dict[str, dict[str, Any]]) -> None:
    if not results:
        print("No recommendations recorded yet.")
    for change_id, doc in sorted(results.items()):
        print(f"\n{change_id}: {doc.get('decision')} / used_skill={doc.get('used_skill')} "
              f"/ requires_human_approval={doc.get('requires_human_approval')}")
        for reason in doc.get("reasons", []) + doc.get("policy_reasons", []):
            print(f"  - {reason}")


def validate(results: dict[str, Any], messages: list[dict[str, Any]]) -> list[str]:
    """Check exact input correlation and deterministic evidence, not model wording."""
    catalog = json.loads((TEMPLATE_DIR / "src" / "data" / "purchasing.json").read_text(encoding="utf-8"))
    errors = []
    for message in messages:
        change_id = message["change_id"]
        doc = results.get(change_id)
        if doc is None:
            errors.append(f"{change_id}: no recommendation recorded")
            continue
        if not isinstance(doc, dict):
            errors.append(f"{change_id}: recommendation is not an object")
            continue
        base_id = "-".join(change_id.split("-")[:2])
        if base_id not in EXPECTED:
            errors.append(f"{change_id}: no expected scenario")
            continue
        decision, used_skill, policy = EXPECTED[base_id]
        for field in ("change_id", "po_id", "supplier_id"):
            if doc.get(field) != message[field]:
                errors.append(f"{change_id}: {field} does not match input")
        digest = hashlib.sha256(json.dumps(message, sort_keys=True).encode("utf-8")).hexdigest()
        if doc.get("input_fingerprint") != digest:
            errors.append(f"{change_id}: input_fingerprint does not match input")
        if doc.get("decision") != decision:
            errors.append(f"{change_id}: expected decision {decision}, got {doc.get('decision')!r}")
        if doc.get("used_skill") is not used_skill:
            errors.append(f"{change_id}: expected used_skill={used_skill}")
        if doc.get("requires_human_approval") is not (decision != "accept_update"):
            errors.append(f"{change_id}: incorrect requires_human_approval")
        if not isinstance(doc.get("summary"), str) or not doc["summary"].strip():
            errors.append(f"{change_id}: no summary")
        reasons = doc.get("reasons")
        if not isinstance(reasons, list) or not reasons or not all(isinstance(r, str) and r.strip() for r in reasons):
            errors.append(f"{change_id}: no valid reasons")
        if doc.get("policy_reasons") != policy:
            errors.append(f"{change_id}: incorrect policy_reasons")
        po = catalog["purchase_orders"][message["po_id"]]
        inventory = catalog["inventory"][po["sku"]]
        proposed = message["proposed_delivery_date"]
        facts = {
            "shortfall_units": max(0, po["outstanding_quantity"] - message["quantity_available"]),
            "inventory_gap_days": max(0, (date.fromisoformat(proposed) - date.fromisoformat(inventory["coverage_until"])).days) if proposed else None,
            "after_required_date": proposed > po["required_by"] if proposed else None,
            "substitution_approved": message["substitute_sku"] in po["approved_substitutions"],
        }
        context = {"purchase_order": po, "inventory": inventory, "policy": catalog["policy"], "facts": facts}
        # JSON comparison distinguishes booleans from integers in the evidence.
        if json.dumps(doc.get("facts"), sort_keys=True) != json.dumps(facts, sort_keys=True):
            errors.append(f"{change_id}: incorrect computed facts")
        if json.dumps(doc.get("context"), sort_keys=True) != json.dumps(context, sort_keys=True):
            errors.append(f"{change_id}: incorrect purchasing evidence snapshot")
        model = doc.get("model_recommendation")
        if (used_skill and model not in {"accept_update", "buyer_review", "request_clarification"}) or (not used_skill and model is not None):
            errors.append(f"{change_id}: incorrect model_recommendation")
    return errors


def _seconds(value: str) -> int:
    seconds = int(value)
    if seconds < 0:
        raise argparse.ArgumentTypeError("seconds must be nonnegative")
    return seconds


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    for name in ("send", "results", "e2e"):
        command = sub.add_parser(name)
        command.add_argument("--local", action="store_true", help="Local settings: Azurite blobs, real Service Bus.")
        if name == "send":
            command.add_argument("--sample", help="One change_id, for example SC-1001.")
        elif name == "results":
            command.add_argument("--id", dest="ids", action="append", help="Exact change_id; repeat for multiple IDs.")
            command.add_argument("--wait", type=_seconds, default=0, help="Poll these exact IDs for up to this many seconds.")
        else:
            command.add_argument("--timeout", type=_seconds, default=600)
    args = parser.parse_args(argv)
    try:
        target = resolve_target(args.local)
        if args.command == "send":
            send(target, load_samples(args.sample))
            return 0
        if args.command == "results":
            ids = args.ids or [sample["change_id"] for sample in load_samples()]
            if any(re.fullmatch(r"[A-Za-z0-9_-]{1,80}", item) is None for item in ids):
                raise ValueError("Each --id must contain 1-80 letters, digits, hyphens, or underscores.")
            ids = list(dict.fromkeys(ids))
            results = wait_for(target, ids, args.wait)
            print_results(results)
            missing = [item for item in ids if item not in results]
            if missing:
                print(f"Missing recommendations: {', '.join(missing)}", file=sys.stderr)
            return 1 if missing else 0
        messages = unique_samples()
        ids = [message["change_id"] for message in messages]
        send(target, messages)
        results = wait_for(target, ids, args.timeout)
        print_results(results)
        errors = validate(results, messages)
        for error in errors:
            print(f"FAIL {error}", file=sys.stderr)
        print("e2e failed" if errors else "e2e passed (blobs only; notification delivery NOT verified)")
        return 1 if errors else 0
    except (ValueError, KeyError, OSError, subprocess.SubprocessError) as exc:
        print(f"Demo failed: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())