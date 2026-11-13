"""Run the daily briefing now and read the published briefings.

The skill normally runs on its timer. To test it without waiting, this script calls the
Functions host's built-in admin endpoint, ``POST /admin/functions/daily_briefing``,
which runs a timer function once (in Azure it needs the app's master key).

Usage (from the template folder):

    python scripts/demo.py run                 # run the briefing now and print it
    python scripts/demo.py results             # print today's briefing
    python scripts/demo.py results --date 2026-05-04
    python scripts/demo.py e2e                 # delete today's briefing, run, and verify

Add ``--local`` to target ``func start`` with Azurite (``--port`` defaults to 7071)
instead of the azd environment.
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
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

TEMPLATE_DIR = Path(__file__).resolve().parent.parent
FUNCTION_NAME = "daily_briefing"
AZURITE = "UseDevelopmentStorage=true"

# What a reader of the sample data would expect to see. A miss is reported but does not
# fail the run, because the model's wording and ranking can reasonably differ.
EXPECTED_MENTIONS = {
    "the overdue Fabrikam RCA in the top 3": ("top_priorities", "fabrikam"),
    "the roadmap deck in the top 3": ("top_priorities", "roadmap"),
    "the phishing email under watch outs": ("watch_outs", "phish"),
    "the interview / budget sync conflict under watch outs": ("watch_outs", "budget"),
}


@dataclass
class Target:
    base_url: str
    container: str
    timezone: str
    master_key: str = ""
    storage_connection: str = ""
    storage_account: str = ""


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


def _local_values() -> dict[str, str]:
    path = TEMPLATE_DIR / "src" / "local.settings.json"
    if not path.exists():
        path = TEMPLATE_DIR / "src" / "local.settings.json.sample"
    return json.loads(path.read_text(encoding="utf-8"))["Values"]


def resolve_target(local: bool, port: int) -> Target:
    if local:
        values = _local_values()
        return Target(
            base_url=f"http://localhost:{port}",
            container=values.get("BRIEFINGS_CONTAINER", "briefings"),
            timezone=values.get("BRIEFING_TIMEZONE", "UTC"),
            storage_connection=values.get("AzureWebJobsStorage", AZURITE),
        )
    values = _azd_values()
    app, group = values["AZURE_FUNCTION_NAME"], values["AZURE_RESOURCE_GROUP"]
    host = _run(["az", "functionapp", "show", "-g", group, "-n", app, "--query", "properties.defaultHostName", "-o", "tsv"])
    key = _run(["az", "functionapp", "keys", "list", "-g", group, "-n", app, "--query", "masterKey", "-o", "tsv"])
    return Target(
        base_url=f"https://{host}",
        container=values["BRIEFINGS_CONTAINER"],
        timezone=values.get("BRIEFING_TIMEZONE", "UTC"),
        master_key=key,
        storage_account=values["AZURE_STORAGE_ACCOUNT_NAME"],
    )


def today(timezone: str) -> str:
    from zoneinfo import ZoneInfo

    try:
        return datetime.now(ZoneInfo(timezone)).date().isoformat()
    except Exception:
        return datetime.now(UTC).date().isoformat()


def _container(target: Target):
    from azure.core.exceptions import ResourceExistsError
    from azure.storage.blob import ContainerClient

    if target.storage_connection:
        container = ContainerClient.from_connection_string(target.storage_connection, target.container)
        try:
            container.create_container()
        except ResourceExistsError:
            pass
        return container

    from azure.identity import DefaultAzureCredential

    credential = DefaultAzureCredential(exclude_interactive_browser_credential=True)
    return ContainerClient(f"https://{target.storage_account}.blob.core.windows.net", target.container, credential=credential)


def trigger(target: Target, attempts: int = 6) -> None:
    """Run the timer function once through the host's admin endpoint (returns 202 Accepted)."""
    request = urllib.request.Request(
        f"{target.base_url}/admin/functions/{FUNCTION_NAME}",
        data=json.dumps({"input": ""}).encode(),
        headers={"Content-Type": "application/json", **({"x-functions-key": target.master_key} if target.master_key else {})},
        method="POST",
    )
    for attempt in range(1, attempts + 1):
        try:
            with urllib.request.urlopen(request, timeout=60) as response:
                print(f"triggered {FUNCTION_NAME}: HTTP {response.status}")
                return
        except (urllib.error.URLError, TimeoutError) as error:
            # A freshly deployed app can return 404/503 until the host has loaded the skill.
            if attempt == attempts:
                raise SystemExit(f"Could not trigger {FUNCTION_NAME}: {error}") from error
            print(f"trigger attempt {attempt} failed ({error}); retrying...")
            time.sleep(15)


def read_briefing(target: Target, day: str) -> dict[str, Any] | None:
    from azure.core.exceptions import ResourceNotFoundError

    try:
        return json.loads(_container(target).download_blob(f"{day}.json").readall())
    except ResourceNotFoundError:
        return None


def delete_briefing(target: Target, day: str) -> None:
    from azure.core.exceptions import ResourceNotFoundError

    container = _container(target)
    for name in (f"{day}.md", f"{day}.json"):
        try:
            container.delete_blob(name)
        except ResourceNotFoundError:
            pass
    print(f"cleared {target.container}/{day}.md so the run publishes a fresh briefing")


def wait_for(target: Target, day: str, timeout: int) -> dict[str, Any] | None:
    deadline = time.monotonic() + timeout
    while True:
        doc = read_briefing(target, day)
        if doc is not None or time.monotonic() >= deadline:
            return doc
        print(f"waiting for {target.container}/{day}.md ...")
        time.sleep(10)


def validate(doc: dict[str, Any] | None, day: str) -> list[str]:
    if doc is None:
        return [f"no briefing published for {day}"]
    errors = []
    priorities = doc.get("top_priorities") or []
    if not 1 <= len(priorities) <= 3:
        errors.append(f"expected 1-3 top priorities, got {len(priorities)}")
    errors += [f"priority {i} has no reason" for i, p in enumerate(priorities, 1) if not p.get("why")]
    if not doc.get("schedule"):
        errors.append("schedule is empty")
    if not doc.get("markdown", "").startswith("# Daily work briefing"):
        errors.append("markdown briefing missing")
    if doc.get("date") != day:
        errors.append(f"briefing date {doc.get('date')!r}, expected {day}")
    if doc.get("data_source") == "sample":
        for label, (field, word) in EXPECTED_MENTIONS.items():
            if word not in json.dumps(doc.get(field, [])).lower():
                print(f"note: expected {label}")
    return errors


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="command", required=True)
    for name in ("run", "results", "e2e"):
        p = sub.add_parser(name)
        p.add_argument("--local", action="store_true", help="Use func start + Azurite / src/local.settings.json.")
        p.add_argument("--port", type=int, default=7071, help="Local Functions host port (with --local).")
        if name == "results":
            p.add_argument("--date", help="Briefing date (YYYY-MM-DD). Defaults to today in BRIEFING_TIMEZONE.")
        else:
            p.add_argument("--timeout", type=int, default=600)
    args = parser.parse_args(argv)
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")

    target = resolve_target(args.local, args.port)
    day = getattr(args, "date", None) or today(target.timezone)

    if args.command == "results":
        doc = read_briefing(target, day)
        print(doc["markdown"] if doc else f"No briefing published for {day} yet.")
        return 0

    if args.command == "e2e":
        delete_briefing(target, day)
    elif read_briefing(target, day):
        print(f"note: {day} is already published; this run should report already_published.")
    trigger(target)
    doc = wait_for(target, day, args.timeout)
    if doc:
        print(doc["markdown"])
    if args.command == "run":
        return 0 if doc else 1

    errors = validate(doc, day)
    for error in errors:
        print(f"FAIL {error}", file=sys.stderr)
    print("e2e passed" if not errors else "e2e failed")
    return 1 if errors else 0


if __name__ == "__main__":
    sys.exit(main())
