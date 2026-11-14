# Copyright (c) Microsoft Corporation.
# Licensed under the MIT License.

"""Send sample IT questions to the helpdesk HTTP test endpoint and check the records.

Usage (from the template folder):

    python scripts/demo.py send                         # ask every sample question
    python scripts/demo.py send --text "How do I add a printer?"
    python scripts/demo.py results                      # list recorded replies, tickets, escalations
    python scripts/demo.py e2e                          # ask unique copies and verify the records

Add ``--local`` to target ``func start`` with Azurite instead of the azd environment.
The HTTP endpoint runs the same tools as the Teams-triggered skill, but records the
reply in Blob Storage (``replies/<message_id>.json``) instead of posting it to Teams.
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import urllib.error
import urllib.request
import uuid
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from pathlib import Path
from typing import Any

TEMPLATE_DIR = Path(__file__).resolve().parent.parent
SAMPLES = TEMPLATE_DIR / "samples" / "questions.json"
AZURITE = "UseDevelopmentStorage=true"
OUTCOMES = {"answered", "ticket_created", "escalated"}
ROUTE = "helpdesk/ask"


@dataclass
class Target:
    base_url: str
    container: str
    function_key: str = ""
    storage_connection: str = ""
    storage_account: str = ""


def _run(*args: str) -> str:
    return subprocess.run(
        list(args), cwd=TEMPLATE_DIR, capture_output=True, text=True, check=True, shell=os.name == "nt"
    ).stdout.strip()


def _azd_values() -> dict[str, str]:
    values = {}
    for line in _run("azd", "env", "get-values").splitlines():
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
            container=values.get("HELPDESK_CONTAINER", "helpdesk"),
            storage_connection=values.get("AzureWebJobsStorage", AZURITE),
        )
    values = _azd_values()
    group, app = values["AZURE_RESOURCE_GROUP"], values["AZURE_FUNCTION_NAME"]
    host = _run("az", "functionapp", "show", "-g", group, "-n", app, "--query", "properties.defaultHostName", "-o", "tsv")
    key = _run("az", "functionapp", "keys", "list", "-g", group, "-n", app, "--query", "functionKeys.default", "-o", "tsv")
    return Target(
        base_url=f"https://{host}",
        container=values["HELPDESK_CONTAINER"],
        function_key=key,
        storage_account=values["AZURE_STORAGE_ACCOUNT_NAME"],
    )


def _container(target: Target):
    from azure.storage.blob import ContainerClient

    if target.storage_connection:
        return ContainerClient.from_connection_string(target.storage_connection, target.container)

    from azure.identity import DefaultAzureCredential

    credential = DefaultAzureCredential(exclude_interactive_browser_credential=True)
    return ContainerClient(f"https://{target.storage_account}.blob.core.windows.net", target.container, credential=credential)


def load_questions() -> list[dict[str, Any]]:
    return json.loads(SAMPLES.read_text(encoding="utf-8"))["questions"]


def ask(target: Target, message_id: str, sender: str, text: str, timeout: int = 600) -> dict[str, Any]:
    request = urllib.request.Request(
        f"{target.base_url}/{ROUTE}",
        data=json.dumps({"message_id": message_id, "from": sender, "text": text}).encode(),
        headers={"Content-Type": "application/json", **({"x-functions-key": target.function_key} if target.function_key else {})},
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return json.loads(response.read())
    except urllib.error.HTTPError as exc:
        return {"error": f"HTTP {exc.code}", "detail": exc.read().decode(errors="replace")[:500]}
    except (urllib.error.URLError, TimeoutError) as exc:
        return {"error": str(exc)}


def ask_all(target: Target, items: list[tuple[str, dict[str, Any]]]) -> dict[str, dict[str, Any]]:
    with ThreadPoolExecutor(max_workers=len(items) or 1) as pool:
        futures = {mid: pool.submit(ask, target, mid, q["from"], q["text"]) for mid, q in items}
    return {mid: future.result() for mid, future in futures.items()}


def read_record(target: Target, prefix: str, message_id: str) -> dict[str, Any] | None:
    from azure.core.exceptions import ResourceNotFoundError

    try:
        return json.loads(_container(target).download_blob(f"{prefix}/{message_id}.json").readall())
    except ResourceNotFoundError:
        return None


def print_answer(message_id: str, answer: dict[str, Any]) -> None:
    if "error" in answer:
        print(f"\n{message_id}: ERROR {answer['error']} {answer.get('detail', '')}")
        return
    extra = " ".join(f"{k}={answer[k]}" for k in ("ticket_id", "escalation_id") if answer.get(k))
    print(f"\n{message_id}: {answer.get('outcome')} {extra}".rstrip())
    print(f"  {answer.get('reply', '').strip()}")
    for source in answer.get("sources", []):
        print(f"  source: {source}")


def print_results(target: Target) -> None:
    from azure.core.exceptions import ResourceNotFoundError

    try:
        blobs = sorted(b.name for b in _container(target).list_blobs())
    except ResourceNotFoundError:
        blobs = []
    if not blobs:
        print("No helpdesk records yet.")
    for name in blobs:
        doc = json.loads(_container(target).download_blob(name).readall())
        summary = doc.get("outcome") or doc.get("ticket_id") or doc.get("escalation_id") or ("delivered" if doc.get("delivered_at") else "")
        print(f"{name}: {summary}")


def validate(target: Target, answers: dict[str, dict[str, Any]], expected: dict[str, list[str]]) -> list[str]:
    errors = []
    for message_id, answer in answers.items():
        if "error" in answer:
            errors.append(f"{message_id}: {answer['error']} {answer.get('detail', '')}".strip())
            continue
        outcome = answer.get("outcome")
        if outcome not in OUTCOMES:
            errors.append(f"{message_id}: unexpected outcome {outcome!r}")
        if not answer.get("reply", "").strip():
            errors.append(f"{message_id}: empty reply")

        reply = read_record(target, "replies", message_id)
        if reply is None:
            errors.append(f"{message_id}: no replies/{message_id}.json recorded")
        elif reply.get("outcome") != outcome:
            errors.append(f"{message_id}: recorded outcome {reply.get('outcome')!r} differs from response {outcome!r}")

        ticket = read_record(target, "tickets", message_id)
        if ticket is not None and (ticket.get("admin_rights_granted") or ticket.get("access_level") != "standard"):
            errors.append(f"{message_id}: ticket grants elevated access")
        if outcome == "ticket_created" and ticket is None:
            errors.append(f"{message_id}: outcome ticket_created but no ticket recorded")
        if outcome == "escalated" and read_record(target, "escalations", message_id) is None:
            errors.append(f"{message_id}: outcome escalated but no escalation recorded")

        hint = expected.get(message_id)
        if hint and outcome not in hint:
            print(f"note: {message_id} outcome {outcome!r}; an analyst would expect {hint}")
    return errors


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="command", required=True)
    for name in ("send", "results", "e2e"):
        p = sub.add_parser(name)
        p.add_argument("--local", action="store_true", help="Use func start + Azurite / src/local.settings.json.")
        p.add_argument("--port", type=int, default=7077, help="Local func port (with --local).")
        if name == "send":
            p.add_argument("--text", help="Ask your own question instead of the samples.")
            p.add_argument("--from", dest="sender", default="Demo User")
            p.add_argument("--message-id", help="Reuse a message ID to see idempotency.")
    args = parser.parse_args(argv)
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8", errors="replace")

    target = resolve_target(args.local, args.port)

    if args.command == "results":
        print_results(target)
        return 0

    run_id = uuid.uuid4().hex[:6]
    if args.command == "send" and args.text:
        message_id = args.message_id or f"demo-{run_id}"
        print_answer(message_id, ask(target, message_id, args.sender, args.text))
        return 0

    prefix = "e2e" if args.command == "e2e" else "demo"
    items = [(f"{prefix}-{run_id}-{q['key']}", q) for q in load_questions()]
    print(f"Asking {len(items)} questions at {target.base_url}/{ROUTE} ...")
    answers = ask_all(target, items)
    for message_id, answer in answers.items():
        print_answer(message_id, answer)
    if args.command == "send":
        return 0

    errors = validate(target, answers, {mid: q.get("expected", []) for mid, q in items})
    for error in errors:
        print(f"FAIL {error}", file=sys.stderr)
    print("e2e passed" if not errors else "e2e failed")
    return 1 if errors else 0


if __name__ == "__main__":
    sys.exit(main())
