# Copyright (c) Microsoft Corporation.
# Licensed under the MIT License.

"""Upload sample insurance claims and read the decisions the workflow records.

Usage (from the template folder):

    python scripts/demo.py upload                    # upload all sample claims to the deployed app
    python scripts/demo.py upload --sample CLM-24002
    python scripts/demo.py upload --file my-claim.json
    python scripts/demo.py results --wait 300        # wait for and print decisions
    python scripts/demo.py e2e                       # upload unique copies and verify decisions

Add ``--local`` to target ``func start`` with Azurite instead of the azd environment.

Supporting documents in ``samples/attachments`` are uploaded to the attachments
container first; each claim form references them by path.
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
ATTACHMENTS_DIR = SAMPLES_DIR / "attachments"
AZURITE = "UseDevelopmentStorage=true"
DECISIONS = {"approve", "request_info", "refer_to_adjuster"}

# What a claims handler would expect for each sample. A mismatch is reported but does not
# fail the run: the model may refer a claim it finds suspicious, which is always allowed.
EXPECTED = {
    "CLM-24001": {"approve"},
    "CLM-24002": {"request_info"},
    "CLM-24003": {"refer_to_adjuster"},
}


@dataclass
class Target:
    inbox_container: str
    attachments_container: str
    decisions_container: str
    auto_approve_limit: float = 10000.0
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
    values = _local_values() if local else _azd_values()
    target = Target(
        inbox_container=values.get("CLAIMS_INBOX_CONTAINER", "claims-inbox"),
        attachments_container=values.get("CLAIMS_ATTACHMENTS_CONTAINER", "claims-attachments"),
        decisions_container=values.get("CLAIMS_DECISIONS_CONTAINER", "claims-decisions"),
        auto_approve_limit=float(values.get("CLAIMS_AUTO_APPROVE_LIMIT") or 10000),
    )
    if local:
        target.storage_connection = values.get("AzureWebJobsStorage", AZURITE)
    else:
        target.storage_account = values["AZURE_STORAGE_ACCOUNT_NAME"]
    return target


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

    from azure.identity import DefaultAzureCredential

    credential = DefaultAzureCredential(exclude_interactive_browser_credential=True)
    return ContainerClient(f"https://{target.storage_account}.blob.core.windows.net", name, credential=credential)


def sample_files(only: str | None = None) -> list[Path]:
    files = [p for p in sorted(SAMPLES_DIR.glob("*.json")) if only is None or p.stem == only]
    if not files:
        raise SystemExit(f"No sample found for {only!r} in {SAMPLES_DIR}")
    return files


def upload_attachments(target: Target) -> None:
    from azure.storage.blob import ContentSettings

    container = _container(target, target.attachments_container)
    for path in sorted(p for p in ATTACHMENTS_DIR.rglob("*") if p.is_file()):
        blob_name = path.relative_to(ATTACHMENTS_DIR).as_posix()
        content_type = "application/pdf" if path.suffix.lower() == ".pdf" else "text/plain; charset=utf-8"
        container.upload_blob(
            blob_name, path.read_bytes(), overwrite=True, content_settings=ContentSettings(content_type=content_type)
        )
    print(f"uploaded supporting documents -> {target.attachments_container}/")


def upload(target: Target, files: list[tuple[str, Path]]) -> None:
    from azure.storage.blob import ContentSettings

    container = _container(target, target.inbox_container)
    for blob_name, path in files:
        container.upload_blob(
            blob_name, path.read_bytes(), overwrite=True,
            content_settings=ContentSettings(content_type="application/json"),
        )
        print(f"uploaded {path.name} -> {target.inbox_container}/{blob_name}")


def read_results(target: Target, decision_ids: list[str] | None = None) -> dict[str, dict[str, Any]]:
    from azure.core.exceptions import ResourceNotFoundError

    container = _container(target, target.decisions_container)
    results: dict[str, dict[str, Any]] = {}
    names = [f"{d}.json" for d in decision_ids] if decision_ids else [b.name for b in container.list_blobs()]
    for name in names:
        try:
            results[name.removesuffix(".json")] = json.loads(container.download_blob(name).readall())
        except ResourceNotFoundError:
            continue
    return results


def wait_for(target: Target, decision_ids: list[str], timeout: int) -> dict[str, dict[str, Any]]:
    deadline = time.monotonic() + timeout
    while True:
        results = read_results(target, decision_ids)
        if len(results) == len(decision_ids) or time.monotonic() >= deadline:
            return results
        print(f"waiting... {len(results)}/{len(decision_ids)} decisions recorded")
        time.sleep(10)


def print_results(results: dict[str, dict[str, Any]]) -> None:
    if not results:
        print("No decisions recorded yet.")
        return
    for decision_id, doc in sorted(results.items()):
        review = "human review" if doc.get("requires_human_review") else "auto"
        print(
            f"\n{decision_id}: {doc.get('decision')} ({review}) claim {doc.get('claim_id')} "
            f"claimed {doc.get('amount_claimed')} / est. payout {doc.get('estimated_payout')}"
        )
        print(f"  workflow {doc.get('workflow_id')}")
        for reason in doc.get("reasons", []):
            print(f"  - {reason}")
        for concern in doc.get("intake_concerns", []):
            print(f"  ? {concern}")
        if doc.get("adjuster_note"):
            print(f"  note: {doc['adjuster_note']}")


def _sample_id(decision_id: str) -> str:
    # e2e copies are named e2e-<run>-<sample id>.
    return decision_id.split("-", 2)[2] if decision_id.startswith("e2e-") else decision_id


def validate(results: dict[str, dict[str, Any]], expected_ids: list[str], limit: float = 10000.0) -> list[str]:
    errors = []
    for decision_id in expected_ids:
        doc = results.get(decision_id)
        if doc is None:
            errors.append(f"{decision_id}: no decision recorded")
            continue
        decision = doc.get("decision")
        if decision not in DECISIONS:
            errors.append(f"{decision_id}: unexpected decision {decision!r}")
        if not doc.get("reasons"):
            errors.append(f"{decision_id}: no reasons")
        if not doc.get("workflow_id"):
            errors.append(f"{decision_id}: no workflow_id; the decision was not written by the workflow")
        if decision == "approve" and doc.get("fraud_signals"):
            errors.append(f"{decision_id}: approved with fraud signals")
        if decision == "approve" and float(doc.get("amount_claimed") or 0) > limit:
            errors.append(f"{decision_id}: approved above the auto-approve limit {limit}")
        if decision != "approve" and not doc.get("requires_human_review"):
            errors.append(f"{decision_id}: {decision} without human review")
        hint = EXPECTED.get(_sample_id(decision_id))
        if hint and decision not in hint:
            print(f"note: {decision_id} decision {decision!r}; a claims handler would expect {sorted(hint)}")
    return errors


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="command", required=True)
    for name in ("upload", "results", "e2e"):
        p = sub.add_parser(name)
        p.add_argument("--local", action="store_true", help="Use Azurite / src/local.settings.json.")
        if name == "upload":
            group = p.add_mutually_exclusive_group()
            group.add_argument("--sample", help="Upload one sample by claim ID, for example CLM-24002.")
            group.add_argument("--file", type=Path, help="Upload your own claim form (JSON).")
        if name == "results":
            p.add_argument("--wait", type=int, default=0, help="Seconds to wait for all samples.")
        if name == "e2e":
            p.add_argument("--timeout", type=int, default=600)
    args = parser.parse_args(argv)

    target = resolve_target(args.local)

    if args.command == "upload":
        upload_attachments(target)
        if args.file:
            upload(target, [(args.file.name, args.file)])
        else:
            upload(target, [(p.name, p) for p in sample_files(args.sample)])
        return 0

    if args.command == "results":
        if args.wait:
            print_results(wait_for(target, [p.stem for p in sample_files()], args.wait))
        else:
            print_results(read_results(target))
        return 0

    run_id = uuid.uuid4().hex[:6]
    files = [(f"e2e-{run_id}-{p.name}", p) for p in sample_files()]
    ids = [Path(name).stem for name, _ in files]
    upload_attachments(target)
    upload(target, files)
    results = wait_for(target, ids, args.timeout)
    print_results(results)
    errors = validate(results, ids, target.auto_approve_limit)
    for error in errors:
        print(f"FAIL {error}", file=sys.stderr)
    print("e2e passed" if not errors else "e2e failed")
    return 1 if errors else 0


if __name__ == "__main__":
    sys.exit(main())
