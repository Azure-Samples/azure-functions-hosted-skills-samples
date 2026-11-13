# Copyright (c) Microsoft Corporation.
# Licensed under the MIT License.

"""Seed customer accounts in Cosmos DB, change their usage, and read the retention actions.

Usage (from the template folder):

    python scripts/demo.py seed                      # upsert the baseline accounts (healthy usage)
    python scripts/demo.py send                      # lower usage on the sample accounts -> change feed -> skill
    python scripts/demo.py send --account ACC-1002   # change one account
    python scripts/demo.py results --wait 300        # wait for and print the actions for the last send
    python scripts/demo.py results                   # print every recorded action
    python scripts/demo.py e2e                       # seed, send, wait, and validate

Add ``--local`` to use the Cosmos DB endpoint in src/local.settings.json instead of the azd
environment. Cosmos DB has local auth disabled, so every command signs in with your Azure
CLI / developer identity (``az login``).
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
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Any

TEMPLATE_DIR = Path(__file__).resolve().parent.parent
SAMPLES_DIR = TEMPLATE_DIR / "samples"
LAST_SEND = TEMPLATE_DIR / ".last-send.json"
DECISIONS = {"retention_offer", "outreach_only"}
STATUSES = {"needs_approval", "draft_ready"}
MAX_DISCOUNT = {"starter": 5, "business": 15, "enterprise": 20}
APPROVAL_ABOVE_DISCOUNT = 10
APPROVAL_ABOVE_VALUE = 10000
_UNSAFE_ID_CHARS = re.compile(r"[^A-Za-z0-9._:-]")

# What a CSM would expect after `send`. None means "no action": usage barely moved.
# A different decision is reported but does not fail the run, because the model's
# judgment can reasonably differ.
EXPECTED: dict[str, set[str] | None] = {
    "ACC-1001": {"outreach_only", "retention_offer"},
    "ACC-1002": {"retention_offer"},
    "ACC-1003": None,
}


@dataclass
class Target:
    endpoint: str
    database: str
    accounts_container: str
    actions_container: str


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
            endpoint=values["COSMOS__accountEndpoint"],
            database=values.get("COSMOS_DATABASE", "retention"),
            accounts_container=values.get("ACCOUNTS_CONTAINER", "accounts"),
            actions_container=values.get("RETENTION_ACTIONS_CONTAINER", "retention-actions"),
        )
    values = _azd_values()
    return Target(
        endpoint=values["COSMOS_ENDPOINT"],
        database=values["COSMOS_DATABASE"],
        accounts_container=values["ACCOUNTS_CONTAINER"],
        actions_container=values["RETENTION_ACTIONS_CONTAINER"],
    )


def _containers(target: Target):
    from azure.cosmos import CosmosClient
    from azure.identity import DefaultAzureCredential

    credential = DefaultAzureCredential(exclude_interactive_browser_credential=True)
    database = CosmosClient(target.endpoint, credential=credential).get_database_client(target.database)
    return database.get_container_client(target.accounts_container), database.get_container_client(target.actions_container)


def _stamp() -> str:
    return datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ") + "-" + uuid.uuid4().hex[:4]


def action_id(account_id: str, snapshot_id: str) -> str:
    """Same id rule as src/tools/record_retention_action.py."""
    return _UNSAFE_ID_CHARS.sub("_", f"{account_id}--{snapshot_id}")


def baseline_accounts(today: date, snapshot_id: str) -> list[dict[str, Any]]:
    accounts = json.loads((SAMPLES_DIR / "accounts.json").read_text(encoding="utf-8"))
    docs = []
    for account in accounts:
        doc = dict(account)
        doc["renewal_date"] = (today + timedelta(days=doc.pop("renewal_in_days"))).isoformat()
        doc["usage"] = {**doc["usage"], "snapshot_id": snapshot_id, "as_of": today.isoformat()}
        docs.append(doc)
    return docs


def apply_usage_change(doc: dict[str, Any], change: dict[str, Any], snapshot_id: str, today: date) -> dict[str, Any]:
    clean = {k: v for k, v in doc.items() if not k.startswith("_")}
    clean["usage"] = {**clean.get("usage", {}), **change, "snapshot_id": snapshot_id, "as_of": today.isoformat()}
    return clean


def seed(target: Target) -> None:
    accounts, _ = _containers(target)
    for doc in baseline_accounts(datetime.now(UTC).date(), f"baseline-{_stamp()}"):
        accounts.upsert_item(doc)
        print(f"seeded {doc['id']} {doc['name']}: {doc['usage']['active_users_30d']} active users, renews {doc['renewal_date']}")


def send(target: Target, only: str | None = None) -> dict[str, str]:
    """Lower usage on the sample accounts. Return {account_id: expected action id}."""
    from azure.cosmos.exceptions import CosmosResourceNotFoundError

    changes = json.loads((SAMPLES_DIR / "usage-drop.json").read_text(encoding="utf-8"))
    if only:
        changes = {k: v for k, v in changes.items() if k == only.upper()}
        if not changes:
            raise SystemExit(f"No usage change for {only!r} in samples/usage-drop.json")
    accounts, _ = _containers(target)
    snapshot_id = f"usage-{_stamp()}"
    today = datetime.now(UTC).date()
    sent = {}
    for account_id, change in changes.items():
        try:
            doc = accounts.read_item(account_id, partition_key=account_id)
        except CosmosResourceNotFoundError:
            raise SystemExit(f"{account_id} not found; run `python scripts/demo.py seed` first") from None
        before = doc.get("usage", {}).get("active_users_30d")
        accounts.upsert_item(apply_usage_change(doc, change, snapshot_id, today))
        sent[account_id] = action_id(account_id, snapshot_id)
        print(f"updated {account_id}: active users {before} -> {change['active_users_30d']} (snapshot {snapshot_id})")
    LAST_SEND.write_text(json.dumps(sent, indent=2), encoding="utf-8")
    return sent


def read_actions(target: Target, ids: dict[str, str] | None = None) -> dict[str, dict[str, Any]]:
    """Read actions by {account_id: action id}, or every action when ``ids`` is None."""
    from azure.cosmos.exceptions import CosmosResourceNotFoundError

    _, actions = _containers(target)
    if ids is None:
        return {d["id"]: d for d in actions.query_items("SELECT * FROM c", enable_cross_partition_query=True)}
    results = {}
    for account_id, item_id in ids.items():
        try:
            results[item_id] = actions.read_item(item_id, partition_key=account_id)
        except CosmosResourceNotFoundError:
            continue
    return results


def wait_for(target: Target, ids: dict[str, str], timeout: int) -> dict[str, dict[str, Any]]:
    deadline = time.monotonic() + timeout
    while True:
        results = read_actions(target, ids)
        if len(results) == len(ids) or time.monotonic() >= deadline:
            return results
        print(f"waiting... {len(results)}/{len(ids)} retention actions recorded")
        time.sleep(10)


def print_results(results: dict[str, dict[str, Any]]) -> None:
    if not results:
        print("No retention actions recorded yet.")
        return
    for doc in sorted(results.values(), key=lambda d: d.get("created_at", "")):
        offer = doc.get("offer", {})
        email = doc.get("email_draft", {})
        print(f"\n{doc.get('account_id')} {doc.get('account_name')}: {doc.get('decision')} ({doc.get('status')}), risk {doc.get('risk_level')}")
        print(f"  offer: {offer.get('type')} {offer.get('discount_percent') or ''} value {offer.get('annual_value')} - {offer.get('summary')}")
        for reason in doc.get("approval_reasons", []):
            print(f"  approval: {reason}")
        print(f"  cause: {doc.get('likely_cause')}")
        print(f"  email to {email.get('to')}: {email.get('subject')}")


def validate(results: dict[str, dict[str, Any]], expected_ids: dict[str, str]) -> list[str]:
    errors = []
    for account_id, item_id in expected_ids.items():
        doc = results.get(item_id)
        if doc is None:
            errors.append(f"{account_id}: no retention action recorded")
            continue
        offer = doc.get("offer", {})
        discount = offer.get("discount_percent") or 0
        if doc.get("decision") not in DECISIONS:
            errors.append(f"{account_id}: unexpected decision {doc.get('decision')!r}")
        if doc.get("status") not in STATUSES:
            errors.append(f"{account_id}: unexpected status {doc.get('status')!r}")
        if discount > MAX_DISCOUNT.get(doc.get("tier", ""), 0):
            errors.append(f"{account_id}: discount {discount}% is above the {doc.get('tier')} limit")
        needs_approval = discount > APPROVAL_ABOVE_DISCOUNT or (offer.get("annual_value") or 0) > APPROVAL_ABOVE_VALUE
        if needs_approval and doc.get("status") != "needs_approval":
            errors.append(f"{account_id}: offer above the approval threshold is not marked needs_approval")
        email = doc.get("email_draft", {})
        if not (email.get("to") and email.get("subject") and email.get("body")):
            errors.append(f"{account_id}: incomplete email draft")
        hint = EXPECTED.get(account_id)
        if hint and doc.get("decision") not in hint:
            print(f"note: {account_id} decision {doc.get('decision')!r}; a CSM would expect {sorted(hint)}")
    return errors


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="command", required=True)
    for name in ("seed", "send", "results", "e2e"):
        p = sub.add_parser(name)
        p.add_argument("--local", action="store_true", help="Use the Cosmos DB endpoint in src/local.settings.json.")
        if name == "send":
            p.add_argument("--account", help="Change one account, for example ACC-1002.")
        if name == "results":
            p.add_argument("--wait", type=int, default=0, help="Seconds to wait for the actions from the last send.")
        if name == "e2e":
            p.add_argument("--timeout", type=int, default=600)
            p.add_argument("--settle", type=int, default=60, help="Seconds between seed and send.")
    args = parser.parse_args(argv)

    target = resolve_target(args.local)

    if args.command == "seed":
        seed(target)
        return 0

    if args.command == "send":
        send(target, args.account)
        return 0

    if args.command == "results":
        if args.wait:
            if not LAST_SEND.exists():
                raise SystemExit("Nothing sent yet; run `python scripts/demo.py send` first")
            ids = {k: v for k, v in json.loads(LAST_SEND.read_text(encoding="utf-8")).items() if EXPECTED.get(k)}
            print_results(wait_for(target, ids, args.wait))
        else:
            print_results(read_actions(target))
        return 0

    seed(target)
    # Let the skill process the baseline change first; healthy accounts need no action.
    print(f"waiting {args.settle}s for the baseline change to be processed")
    time.sleep(args.settle)
    sent = send(target)
    expected = {k: v for k, v in sent.items() if EXPECTED.get(k)}
    results = wait_for(target, expected, args.timeout)
    print_results(results)
    errors = validate(results, expected)
    for account_id, item_id in sent.items():
        if account_id not in expected and read_actions(target, {account_id: item_id}):
            print(f"note: {account_id} got a retention action; a CSM would expect none for a small change")
    for error in errors:
        print(f"FAIL {error}", file=sys.stderr)
    print("e2e passed" if not errors else "e2e failed")
    return 1 if errors else 0


if __name__ == "__main__":
    sys.exit(main())
