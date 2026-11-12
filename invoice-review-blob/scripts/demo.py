"""Upload sample invoices and read the hosted skill's reviews.

Usage (from the template folder):

    python scripts/demo.py upload                    # upload all sample invoices to the deployed app
    python scripts/demo.py upload --sample INV-NW-2002
    python scripts/demo.py upload --file my-invoice.pdf
    python scripts/demo.py results --wait 300        # wait for and print reviews
    python scripts/demo.py e2e                       # upload unique copies and verify reviews

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
DECISIONS = {"approve_for_payment", "hold_for_review", "reject"}

# What an AP reviewer would expect for each sample. A mismatch is reported but does not
# fail the run, because the model's judgment can reasonably differ on borderline cases.
EXPECTED = {
    "INV-FAB-1001": {"approve_for_payment"},
    "INV-NW-2002": {"hold_for_review"},
    "INV-TS-3003": {"reject"},
}


@dataclass
class Target:
    invoices_container: str
    reviews_container: str
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
            invoices_container=values.get("INVOICES_CONTAINER", "invoices"),
            reviews_container=values.get("REVIEWS_CONTAINER", "invoice-reviews"),
            storage_connection=values.get("AzureWebJobsStorage", AZURITE),
        )
    values = _azd_values()
    return Target(
        invoices_container=values["INVOICES_CONTAINER"],
        reviews_container=values["REVIEWS_CONTAINER"],
        storage_account=values["AZURE_STORAGE_ACCOUNT_NAME"],
    )


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
    files = [p for p in sorted(SAMPLES_DIR.glob("*.pdf")) if only is None or p.stem == only]
    if not files:
        raise SystemExit(f"No sample found for {only!r} in {SAMPLES_DIR}")
    return files


def upload(target: Target, files: list[tuple[str, Path]]) -> None:
    from azure.storage.blob import ContentSettings

    container = _container(target, target.invoices_container)
    for blob_name, path in files:
        content_type = "application/pdf" if path.suffix.lower() == ".pdf" else "application/octet-stream"
        container.upload_blob(
            blob_name, path.read_bytes(), overwrite=True, content_settings=ContentSettings(content_type=content_type)
        )
        print(f"uploaded {path.name} -> {target.invoices_container}/{blob_name}")


def read_results(target: Target, review_ids: list[str] | None = None) -> dict[str, dict[str, Any]]:
    from azure.core.exceptions import ResourceNotFoundError

    container = _container(target, target.reviews_container)
    results: dict[str, dict[str, Any]] = {}
    names = [f"{r}.json" for r in review_ids] if review_ids else [b.name for b in container.list_blobs()]
    for name in names:
        try:
            results[name.removesuffix(".json")] = json.loads(container.download_blob(name).readall())
        except ResourceNotFoundError:
            continue
    return results


def wait_for(target: Target, review_ids: list[str], timeout: int) -> dict[str, dict[str, Any]]:
    deadline = time.monotonic() + timeout
    while True:
        results = read_results(target, review_ids)
        if len(results) == len(review_ids) or time.monotonic() >= deadline:
            return results
        print(f"waiting... {len(results)}/{len(review_ids)} reviews recorded")
        time.sleep(10)


def print_results(results: dict[str, dict[str, Any]]) -> None:
    if not results:
        print("No reviews recorded yet.")
        return
    for review_id, doc in sorted(results.items()):
        approval = "needs approval" if doc.get("requires_human_approval") else "auto"
        print(
            f"\n{review_id}: {doc.get('decision')} ({approval}) "
            f"total {doc.get('invoice_total')} / in dispute {doc.get('amount_in_dispute')}"
        )
        for item in doc.get("discrepancies", []):
            print(f"  ! {item}")
        for reason in doc.get("reasons", []):
            print(f"  - {reason}")


def _sample_id(review_id: str) -> str:
    # e2e copies are named e2e-<run>-<sample id>.
    return review_id.split("-", 2)[2] if review_id.startswith("e2e-") else review_id


def validate(results: dict[str, dict[str, Any]], expected_ids: list[str]) -> list[str]:
    errors = []
    for review_id in expected_ids:
        doc = results.get(review_id)
        if doc is None:
            errors.append(f"{review_id}: no review recorded")
            continue
        if doc.get("decision") not in DECISIONS:
            errors.append(f"{review_id}: unexpected decision {doc.get('decision')!r}")
        if not doc.get("reasons"):
            errors.append(f"{review_id}: no reasons")
        if doc.get("decision") != "approve_for_payment" and not doc.get("requires_human_approval"):
            errors.append(f"{review_id}: {doc.get('decision')} without human approval")
        hint = EXPECTED.get(_sample_id(review_id))
        if hint and doc.get("decision") not in hint:
            print(f"note: {review_id} decision {doc.get('decision')!r}; a reviewer would expect {sorted(hint)}")
    return errors


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="command", required=True)
    for name in ("upload", "results", "e2e"):
        p = sub.add_parser(name)
        p.add_argument("--local", action="store_true", help="Use Azurite / src/local.settings.json.")
        if name == "upload":
            group = p.add_mutually_exclusive_group()
            group.add_argument("--sample", help="Upload one sample by invoice number, for example INV-NW-2002.")
            group.add_argument("--file", type=Path, help="Upload your own invoice file (PDF, JSON, or text).")
        if name == "results":
            p.add_argument("--wait", type=int, default=0, help="Seconds to wait for all samples.")
        if name == "e2e":
            p.add_argument("--timeout", type=int, default=600)
    args = parser.parse_args(argv)

    target = resolve_target(args.local)

    if args.command == "upload":
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
    upload(target, files)
    results = wait_for(target, ids, args.timeout)
    print_results(results)
    errors = validate(results, ids)
    for error in errors:
        print(f"FAIL {error}", file=sys.stderr)
    print("e2e passed" if not errors else "e2e failed")
    return 1 if errors else 0


if __name__ == "__main__":
    sys.exit(main())
