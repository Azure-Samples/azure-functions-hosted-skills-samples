# Copyright (c) Microsoft Corporation.
# Licensed under the MIT License.

"""Add and update sample products in Cosmos DB and check that the hosted skill ran.

Usage (from the template folder):

    python scripts/demo.py seed      # upsert samples/products.json (new products)
    python scripts/demo.py update    # apply samples/updates.json (low stock, price cut)
    python scripts/demo.py e2e       # upsert unique products and wait for a successful run

Every write lands in the change feed and starts the product_watch hosted skill.
The summary is in the function logs (and in your inbox when TO_EMAIL is set).
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

TEMPLATE_DIR = Path(__file__).resolve().parent.parent
SAMPLES_DIR = TEMPLATE_DIR / "samples"
FUNCTION_NAME = "product_watch"
DATABASE = "starter"
CONTAINER = "products"


def azd_values() -> dict[str, str]:
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


def credential():
    from azure.identity import DefaultAzureCredential

    return DefaultAzureCredential(exclude_interactive_browser_credential=True)


def products_container(values: dict[str, str] | None = None):
    from azure.cosmos import CosmosClient

    endpoint = (values or azd_values())["COSMOS_ENDPOINT"]
    return CosmosClient(endpoint, credential()).get_database_client(DATABASE).get_container_client(CONTAINER)


def load(name: str) -> list[dict[str, Any]]:
    return json.loads((SAMPLES_DIR / name).read_text(encoding="utf-8"))


def seed(products: list[dict[str, Any]], container=None) -> None:
    container = container or products_container()
    for product in products:
        container.upsert_item(product)
        print(f"upserted {product['id']} ({product['name']})")


def update(changes: list[dict[str, Any]]) -> None:
    """Apply each change and record the earlier values in ``previous`` so the skill can compare."""
    container = products_container()
    for change in changes:
        product = container.read_item(change["id"], partition_key=change["id"])
        fields = {k: v for k, v in change.items() if k not in ("id", "note")}
        product["previous"] = {k: product.get(k) for k in fields}
        product.update(fields)
        product["note"] = change.get("note", "")
        container.upsert_item(product)
        print(f"updated {product['id']}: " + ", ".join(f"{k} {product['previous'][k]} -> {v}" for k, v in fields.items()))


def count_runs(resource_id: str, since: datetime) -> tuple[int, int]:
    """Return (succeeded, failed) runs of the function since ``since`` from Application Insights."""
    from azure.monitor.query import LogsQueryClient, LogsQueryStatus

    query = (
        f"requests | where timestamp > datetime({since.isoformat()}) and operation_Name == '{FUNCTION_NAME}'"
        " | summarize succeeded = countif(success == true), failed = countif(success == false)"
    )
    result = LogsQueryClient(credential()).query_resource(resource_id, query, timespan=timedelta(days=1))
    if result.status != LogsQueryStatus.SUCCESS or not result.tables or not result.tables[0].rows:
        return 0, 0
    succeeded, failed = result.tables[0].rows[0]
    return int(succeeded), int(failed)


def e2e(timeout: int) -> int:
    values = azd_values()
    run_id = uuid.uuid4().hex[:6]
    products = [{**p, "id": f"{p['id']}-{run_id}"} for p in load("products.json")]
    started = datetime.now(timezone.utc) - timedelta(seconds=30)
    seed(products, products_container(values))
    print("Application Insights can lag a few minutes behind the run.")
    deadline = time.monotonic() + timeout
    succeeded = failed = 0
    while time.monotonic() < deadline:
        succeeded, failed = count_runs(values["APPLICATIONINSIGHTS_RESOURCE_ID"], started)
        print(f"waiting... {succeeded} runs succeeded, {failed} failed")
        if succeeded >= 1:
            print("e2e passed")
            return 0
        time.sleep(20)
    print(f"FAIL no successful run ({failed} failed)", file=sys.stderr)
    print("e2e failed")
    return 1


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("seed")
    sub.add_parser("update")
    p = sub.add_parser("e2e")
    p.add_argument("--timeout", type=int, default=900)
    args = parser.parse_args(argv)

    if args.command == "seed":
        seed(load("products.json"))
        return 0
    if args.command == "update":
        update(load("updates.json"))
        return 0
    return e2e(args.timeout)


if __name__ == "__main__":
    sys.exit(main())
