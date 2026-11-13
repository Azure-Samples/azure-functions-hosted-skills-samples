"""Seed policies, send sample expense requests, and read the routed decisions.

Usage (from the template folder):

    python scripts/demo.py seed                      # upload policies/*.md (missing ones; --force to overwrite)
    python scripts/demo.py send                      # send the travel, client-dinner, equipment, and missing-amount samples
    python scripts/demo.py send --file samples/foreign-currency.txt
    python scripts/demo.py send --text "Team lunch ran about $45"
    python scripts/demo.py results                   # peek the three output queues (--clear to remove them)
    python scripts/demo.py set-policy strict         # swap travel-policy.md for samples/strict-travel-policy.md
    python scripts/demo.py set-policy default        # restore policies/travel-policy.md
    python scripts/demo.py e2e                       # send, verify routing, swap the travel policy, verify again

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
POLICIES_DIR = TEMPLATE_DIR / "policies"
STRICT_TRAVEL_POLICY = SAMPLES_DIR / "strict-travel-policy.md"
AZURITE = "UseDevelopmentStorage=true"
REQUESTS_QUEUE = "expense-requests"
OUTPUT_QUEUES = ["expense-approved", "expense-review", "expense-flagged"]
DEFAULT_SAMPLES = ["travel", "client-dinner", "equipment", "missing-amount"]

# Expected routing with the default policies: (sample, queue, policy document).
# The same 450 USD is approved as travel and equipment but reviewed as a client dinner.
EXPECTED = {
    "travel": ("expense-approved", "travel-policy.md"),
    "client-dinner": ("expense-review", "meals-entertainment-policy.md"),
    "equipment": ("expense-approved", "equipment-software-policy.md"),
    "missing-amount": ("expense-flagged", None),
}
# With samples/strict-travel-policy.md the travel auto-approve ceiling drops to 250 USD, so the
# 450 USD flight goes to review. Equipment uses a different document and must not change.
EXPECTED_STRICT = {
    "travel": ("expense-review", "travel-policy.md"),
    "equipment": ("expense-approved", "equipment-software-policy.md"),
}
SAMPLE_TAGS = {"travel": "TRV", "client-dinner": "DIN", "equipment": "EQP", "missing-amount": "MIS"}


@dataclass
class Target:
    policies_container: str
    decisions_container: str
    connection_string: str = ""
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
            policies_container=values.get("POLICIES_CONTAINER", "policies"),
            decisions_container=values.get("DECISIONS_CONTAINER", "decisions"),
            connection_string=values.get("AzureWebJobsStorage", AZURITE),
        )
    values = _azd_values()
    return Target(
        policies_container=values.get("POLICIES_CONTAINER", "policies"),
        decisions_container=values.get("DECISIONS_CONTAINER", "decisions"),
        storage_account=values["AZURE_STORAGE_ACCOUNT_NAME"],
    )


def _credential():
    from azure.identity import DefaultAzureCredential

    return DefaultAzureCredential(exclude_interactive_browser_credential=True)


def _container(target: Target, name: str):
    from azure.core.exceptions import ResourceExistsError
    from azure.storage.blob import ContainerClient

    if target.connection_string:
        container = ContainerClient.from_connection_string(target.connection_string, name)
        try:
            container.create_container()
        except ResourceExistsError:
            pass
        return container
    return ContainerClient(f"https://{target.storage_account}.blob.core.windows.net", name, credential=_credential())


def _queue(target: Target, name: str):
    from azure.core.exceptions import ResourceExistsError
    from azure.storage.queue import QueueClient

    if target.connection_string:
        queue = QueueClient.from_connection_string(target.connection_string, name)
        try:
            queue.create_queue()
        except ResourceExistsError:
            pass
        return queue
    return QueueClient(f"https://{target.storage_account}.queue.core.windows.net", name, credential=_credential())


def upload_policy(target: Target, name: str, path: Path) -> None:
    from azure.storage.blob import ContentSettings

    _container(target, target.policies_container).upload_blob(
        name, path.read_bytes(), overwrite=True, content_settings=ContentSettings(content_type="text/markdown")
    )


def seed(target: Target, force: bool = False) -> None:
    container = _container(target, target.policies_container)
    existing = {blob.name for blob in container.list_blobs()}
    for path in sorted(POLICIES_DIR.glob("*.md")):
        if path.name in existing and not force:
            print(f"kept     {target.policies_container}/{path.name} (already exists; --force to overwrite)")
            continue
        upload_policy(target, path.name, path)
        print(f"uploaded {target.policies_container}/{path.name}")


def set_policy(target: Target, mode: str) -> None:
    source = STRICT_TRAVEL_POLICY if mode == "strict" else POLICIES_DIR / "travel-policy.md"
    upload_policy(target, "travel-policy.md", source)
    print(f"{target.policies_container}/travel-policy.md <- {source.relative_to(TEMPLATE_DIR).as_posix()}")


def load_sample(name: str) -> str:
    path = SAMPLES_DIR / f"{name}.txt"
    if not path.exists():
        raise SystemExit(f"No sample {path.name} in {SAMPLES_DIR}")
    return path.read_text(encoding="utf-8").strip()


def with_expense_id(expense_id: str, text: str) -> str:
    return f"Expense ID: {expense_id}\n{text}"


def send(target: Target, messages: list[str]) -> None:
    queue = _queue(target, REQUESTS_QUEUE)
    for text in messages:
        queue.send_message(text)
        print(f"sent -> {REQUESTS_QUEUE}: {text.splitlines()[0][:90]}")


def peek_decisions(target: Target) -> list[dict[str, Any]]:
    rows = []
    for name in OUTPUT_QUEUES:
        for message in _queue(target, name).peek_messages(max_messages=32):
            try:
                body = json.loads(message.content)
            except (TypeError, ValueError):
                body = {"reason": str(message.content)}
            rows.append({"queue": name, **body})
    return rows


def clear_outputs(target: Target) -> None:
    for name in OUTPUT_QUEUES:
        _queue(target, name).clear_messages()
    print(f"cleared {', '.join(OUTPUT_QUEUES)}")


def read_markers(target: Target, expense_ids: list[str]) -> dict[str, dict[str, Any]]:
    from azure.core.exceptions import ResourceNotFoundError

    container = _container(target, target.decisions_container)
    found = {}
    for expense_id in expense_ids:
        try:
            found[expense_id] = json.loads(container.download_blob(f"{expense_id}.json").readall())
        except ResourceNotFoundError:
            continue
    return found


def wait_for(target: Target, expense_ids: list[str], timeout: int) -> dict[str, dict[str, Any]]:
    deadline = time.monotonic() + timeout
    while True:
        found = read_markers(target, expense_ids)
        if len(found) == len(expense_ids) or time.monotonic() >= deadline:
            return found
        print(f"waiting... {len(found)}/{len(expense_ids)} decisions routed")
        time.sleep(10)


def format_table(rows: list[dict[str, Any]]) -> str:
    if not rows:
        return "No decisions in the output queues yet."
    columns = ["queue", "expenseId", "amount", "currency", "category", "policyApplied", "reason"]
    lines = [" | ".join(columns)]
    for row in rows:
        values = []
        for column in columns:
            value = row.get(column)
            text = "" if value is None else str(value)
            values.append(text if column == "reason" else text[:40])
        lines.append(" | ".join(values))
    return "\n".join(lines)


def validate(
    markers: dict[str, dict[str, Any]],
    queued: list[dict[str, Any]],
    expected: dict[str, tuple[str, str | None]],
) -> list[str]:
    """Check each expense ID was routed once to the expected queue under the expected policy."""
    errors = []
    for expense_id, (queue, policy) in expected.items():
        marker = markers.get(expense_id)
        if marker is None:
            errors.append(f"{expense_id}: no decision routed")
            continue
        if marker.get("routedTo") != queue:
            errors.append(f"{expense_id}: routed to {marker.get('routedTo')!r}, expected {queue!r} ({marker.get('reason')})")
        if policy and marker.get("policyApplied") != policy:
            errors.append(f"{expense_id}: applied {marker.get('policyApplied')!r}, expected {policy!r}")
        in_queue = [row["queue"] for row in queued if row.get("expenseId") == expense_id]
        if in_queue != [marker.get("routedTo")]:
            errors.append(f"{expense_id}: expected one message in {marker.get('routedTo')!r}, found {in_queue}")
    return errors


def run_round(target: Target, label: str, samples: list[str], expected: dict, run_id: str, timeout: int) -> list[str]:
    ids = {name: f"EXP-{SAMPLE_TAGS[name]}-{label}{run_id}" for name in samples}
    send(target, [with_expense_id(ids[name], load_sample(name)) for name in samples])
    markers = wait_for(target, list(ids.values()), timeout)
    queued = peek_decisions(target)
    print(format_table([row for row in queued if row.get("expenseId") in ids.values()]))
    return validate(markers, queued, {ids[name]: expected[name] for name in samples})


def e2e(target: Target, timeout: int) -> int:
    run_id = uuid.uuid4().hex[:6]
    clear_outputs(target)
    seed(target, force=True)

    print("\n--- default policies ---")
    errors = run_round(target, "D", DEFAULT_SAMPLES, EXPECTED, run_id, timeout)

    print("\n--- strict travel policy (no redeploy) ---")
    try:
        set_policy(target, "strict")
        errors += run_round(target, "S", list(EXPECTED_STRICT), EXPECTED_STRICT, run_id, timeout)
    finally:
        set_policy(target, "default")

    for error in errors:
        print(f"FAIL {error}", file=sys.stderr)
    print("e2e passed" if not errors else "e2e failed")
    return 1 if errors else 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="command", required=True)
    for name in ("seed", "send", "results", "set-policy", "e2e"):
        p = sub.add_parser(name)
        p.add_argument("--local", action="store_true", help="Use Azurite / src/local.settings.json.")
        if name == "seed":
            p.add_argument("--force", action="store_true", help="Overwrite documents that already exist.")
        if name == "send":
            group = p.add_mutually_exclusive_group()
            group.add_argument("--file", type=Path, help="Send the contents of a file as one message.")
            group.add_argument("--sample", choices=sorted(p.stem for p in SAMPLES_DIR.glob("*.txt")))
            group.add_argument("--text", help="Send this text as one message.")
        if name == "results":
            p.add_argument("--clear", action="store_true", help="Remove the messages after printing them.")
        if name == "set-policy":
            p.add_argument("mode", choices=["strict", "default"])
        if name == "e2e":
            p.add_argument("--timeout", type=int, default=600)
    args = parser.parse_args(argv)

    target = resolve_target(args.local)

    if args.command == "seed":
        seed(target, args.force)
    elif args.command == "send":
        if args.file:
            messages = [args.file.read_text(encoding="utf-8").strip()]
        elif args.text:
            messages = [args.text]
        else:
            messages = [load_sample(name) for name in ([args.sample] if args.sample else DEFAULT_SAMPLES)]
        send(target, messages)
    elif args.command == "results":
        print(format_table(peek_decisions(target)))
        if args.clear:
            clear_outputs(target)
    elif args.command == "set-policy":
        set_policy(target, args.mode)
    else:
        return e2e(target, args.timeout)
    return 0


if __name__ == "__main__":
    sys.exit(main())
