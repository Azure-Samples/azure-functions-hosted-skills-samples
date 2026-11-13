# Copyright (c) Microsoft Corporation.
# Licensed under the MIT License.

"""Start contract reviews, approve them, and check their decisions.

Usage (from the template folder):

    python scripts/demo.py send                         # start a review for every sample contract
    python scripts/demo.py send --sample CTR-2026-0187
    python scripts/demo.py send --file my-contract.json
    python scripts/demo.py status <instanceId>          # stage, custom status, and decision
    python scripts/demo.py approve <instanceId> --decision approved --approver alex@contoso.com
    python scripts/demo.py e2e                          # run every sample through the full process

Add ``--local`` to target ``func start`` on http://localhost:7074 instead of the azd environment.
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Any

TEMPLATE_DIR = Path(__file__).resolve().parent.parent
SAMPLES_DIR = TEMPLATE_DIR / "samples"
LOCAL_URL = "http://localhost:7074"
AWAITING = "awaiting_legal_approval"
DEMO_APPROVER = "demo-legal@contoso.com"

# What the process must do with each sample. The low-risk contract matches the playbook
# exactly, so it is auto-approved; the high-risk one waits for a lawyer; the incomplete
# one fails validation before any model call.
EXPECTED = {
    "CTR-2026-0142": "auto_approved",
    "CTR-2026-0187": "approved",
    "CTR-2026-0203": "invalid",
}


@dataclass
class Target:
    base_url: str
    key: str = ""


def _run(args: list[str]) -> str:
    return subprocess.run(
        args, cwd=TEMPLATE_DIR, capture_output=True, text=True, check=True, shell=os.name == "nt"
    ).stdout.strip()


def _azd_values() -> dict[str, str]:
    values = {}
    for line in _run(["azd", "env", "get-values"]).splitlines():
        key, sep, value = line.partition("=")
        if sep:
            values[key.strip()] = value.strip().strip('"')
    return values


def resolve_target(local: bool, port: int) -> Target:
    if local:
        return Target(base_url=f"http://localhost:{port}")
    values = _azd_values()
    group, name = values["AZURE_RESOURCE_GROUP"], values["AZURE_FUNCTION_NAME"]
    # Flex Consumption apps report their host name only under properties.defaultHostName.
    host = _run(["az", "functionapp", "show", "-g", group, "-n", name, "--query", "properties.defaultHostName", "-o", "tsv"])
    key = _run(["az", "functionapp", "keys", "list", "-g", group, "-n", name, "--query", "functionKeys.default", "-o", "tsv"])
    return Target(base_url=f"https://{host}", key=key)


def call(target: Target, method: str, path: str, body: Any = None) -> tuple[int, Any]:
    data = json.dumps(body).encode() if body is not None else None
    request = urllib.request.Request(f"{target.base_url}/{path.lstrip('/')}", data=data, method=method)
    request.add_header("Content-Type", "application/json")
    if target.key:
        request.add_header("x-functions-key", target.key)
    try:
        with urllib.request.urlopen(request, timeout=120) as response:
            status, raw = response.status, response.read()
    except urllib.error.HTTPError as exc:
        status, raw = exc.code, exc.read()
    try:
        return status, json.loads(raw) if raw else None
    except json.JSONDecodeError:
        return status, raw.decode(errors="replace")


def sample_files(only: str | None = None) -> list[Path]:
    files = [p for p in sorted(SAMPLES_DIR.glob("*.json")) if only is None or p.stem.startswith(only)]
    if not files:
        raise SystemExit(f"No sample found for {only!r} in {SAMPLES_DIR}")
    return files


def start(target: Target, path: Path) -> str:
    status, body = call(target, "POST", "contracts", json.loads(path.read_text(encoding="utf-8")))
    if status != 202:
        raise SystemExit(f"Starting {path.name} failed: HTTP {status} {body}")
    print(f"started {path.stem} -> instance {body['instanceId']}")
    return body["instanceId"]


def status_of(target: Target, instance_id: str) -> dict[str, Any]:
    status, body = call(target, "GET", f"contracts/{instance_id}")
    if status != 200:
        raise SystemExit(f"Status for {instance_id} failed: HTTP {status} {body}")
    return body


def approve(target: Target, instance_id: str, decision: str, approver: str, comment: str) -> None:
    status, body = call(
        target, "POST", f"contracts/{instance_id}/approve",
        {"decision": decision, "approver": approver, "comment": comment},
    )
    if status != 202:
        raise SystemExit(f"Approval for {instance_id} failed: HTTP {status} {body}")
    print(f"{decision} {instance_id} as {approver}")


def stage(doc: dict[str, Any]) -> str:
    return (doc.get("customStatus") or {}).get("stage", "")


def is_finished(doc: dict[str, Any]) -> bool:
    return doc.get("runtimeStatus") in ("COMPLETED", "FAILED", "TERMINATED")


def wait_until(target: Target, instance_id: str, done, timeout: int) -> dict[str, Any]:
    deadline = time.monotonic() + timeout
    while True:
        doc = status_of(target, instance_id)
        if done(doc) or is_finished(doc) or time.monotonic() >= deadline:
            return doc
        print(f"  {instance_id}: {doc.get('runtimeStatus')} / {stage(doc) or '-'}")
        time.sleep(10)


def print_status(name: str, doc: dict[str, Any]) -> None:
    output = doc.get("output") or {}
    print(f"\n{name} [{doc.get('instanceId')}]: {doc.get('runtimeStatus')} / {stage(doc) or '-'}")
    if not output:
        print(json.dumps(doc.get("customStatus"), indent=2))
        return
    print(f"  outcome: {output.get('outcome')} (decided by {output.get('decided_by')})")
    if output.get("risk_level") is not None:
        print(f"  risk: {output.get('risk_level')} ({output.get('risk_score')}), skill recommended {output.get('recommendation')}")
    for reason in output.get("legal_review_reasons", []):
        print(f"  ! {reason}")
    for deviation in output.get("deviations", []):
        print(f"  - [{deviation.get('severity')}] {deviation.get('clause')}: {deviation.get('contract_position')}")
    for error in output.get("validation_errors", []):
        print(f"  x {error}")
    if output.get("summary"):
        print(f"  summary: {output['summary']}")
    if output.get("record"):
        print(f"  record: {output['record']}")


def validate(results: dict[str, dict[str, Any]]) -> list[str]:
    """Check each sample's final orchestration status against the process rules."""
    errors = []
    for contract_id, expected in EXPECTED.items():
        doc = results.get(contract_id)
        if doc is None:
            errors.append(f"{contract_id}: not run")
            continue
        if doc.get("runtimeStatus") != "COMPLETED":
            errors.append(f"{contract_id}: runtime status {doc.get('runtimeStatus')}")
            continue
        output = doc.get("output") or {}
        if output.get("outcome") != expected:
            errors.append(f"{contract_id}: outcome {output.get('outcome')!r}, expected {expected!r}")
        if not output.get("record"):
            errors.append(f"{contract_id}: no decision record written")
        if expected == "invalid":
            if not output.get("validation_errors"):
                errors.append(f"{contract_id}: invalid without validation errors")
            if output.get("risk_level") is not None:
                errors.append(f"{contract_id}: invalid contract was sent to the model")
            continue
        if output.get("risk_level") not in ("low", "medium", "high") or not output.get("summary"):
            errors.append(f"{contract_id}: missing risk assessment from the hosted skill")
        if expected == "auto_approved" and output.get("legal_review_reasons"):
            errors.append(f"{contract_id}: auto-approved with legal review reasons")
        if expected == "approved":
            if not output.get("legal_review_reasons") or not output.get("deviations"):
                errors.append(f"{contract_id}: routed to legal without reasons or deviations")
            if (output.get("approval") or {}).get("approver") != DEMO_APPROVER:
                errors.append(f"{contract_id}: approval not recorded")
    return errors


def e2e(target: Target, timeout: int) -> int:
    instances = {json.loads(path.read_text(encoding="utf-8"))["contract_id"]: start(target, path) for path in sample_files()}
    high_risk = instances["CTR-2026-0187"]

    print("\nwaiting for the high-risk contract to reach legal review...")
    doc = wait_until(target, high_risk, lambda d: stage(d) == AWAITING, timeout)
    if stage(doc) == AWAITING:
        print(f"  legal review task: {doc['customStatus'].get('task')}")
        for reason in doc["customStatus"].get("reasons", []):
            print(f"  ! {reason}")
        approve(target, high_risk, "approved", DEMO_APPROVER, "Approved with redlines to sections 3, 11, 12, 13 and 18.")

    results = {}
    for contract_id, instance_id in instances.items():
        results[contract_id] = wait_until(target, instance_id, is_finished, timeout)
        print_status(contract_id, results[contract_id])

    errors = validate(results)
    for error in errors:
        print(f"FAIL {error}", file=sys.stderr)
    print("e2e passed" if not errors else "e2e failed")
    return 1 if errors else 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="command", required=True)
    for name in ("send", "status", "approve", "e2e"):
        p = sub.add_parser(name)
        p.add_argument("--local", action="store_true", help="Use func start on localhost.")
        p.add_argument("--port", type=int, default=7074, help="Local port (with --local).")
        if name == "send":
            group = p.add_mutually_exclusive_group()
            group.add_argument("--sample", help="Start one sample by contract ID, for example CTR-2026-0187.")
            group.add_argument("--file", type=Path, help="Start a review for your own contract JSON file.")
        if name in ("status", "approve"):
            p.add_argument("instance_id")
        if name == "approve":
            p.add_argument("--decision", choices=("approved", "rejected"), default="approved")
            p.add_argument("--approver", default=DEMO_APPROVER)
            p.add_argument("--comment", default="")
        if name == "e2e":
            p.add_argument("--timeout", type=int, default=600)
    args = parser.parse_args(argv)

    target = resolve_target(args.local, args.port)
    if args.command == "send":
        for path in [args.file] if args.file else sample_files(args.sample):
            start(target, path)
        return 0
    if args.command == "status":
        print(json.dumps(status_of(target, args.instance_id), indent=2))
        return 0
    if args.command == "approve":
        approve(target, args.instance_id, args.decision, args.approver, args.comment)
        return 0
    return e2e(target, args.timeout)


if __name__ == "__main__":
    sys.exit(main())
