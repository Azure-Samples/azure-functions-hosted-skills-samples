"""azd postdeploy hook: seed the policy documents into Blob Storage.

Uploads each ``policies/*.md`` file that is not already in the ``policies`` container.
Existing documents are left alone, so policy edits made in Blob Storage survive a
redeploy. Use ``python scripts/demo.py seed --force`` to reset them.

Uses the Azure CLI with your signed-in account (``--auth-mode login``) so the hook needs
no extra Python packages. azd grants that account Storage Blob Data Contributor.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

POLICIES_DIR = Path(__file__).resolve().parent.parent / "policies"


def az(*args: str) -> str:
    executable = shutil.which("az")
    if executable is None:
        raise SystemExit("Azure CLI (az) is required for the postdeploy hook: https://aka.ms/azure-cli")
    result = subprocess.run([executable, *args, "--only-show-errors"], capture_output=True, text=True)
    if result.returncode != 0:
        raise RuntimeError(result.stderr.strip() or result.stdout.strip() or f"az exited with {result.returncode}")
    return result.stdout.strip()


def existing_policies(account: str, container: str, attempts: int = 12, delay: int = 15) -> set[str]:
    # New role assignments can take a few minutes to reach the data plane.
    for attempt in range(1, attempts + 1):
        try:
            out = az("storage", "blob", "list", "--account-name", account, "--container-name", container,
                     "--auth-mode", "login", "--query", "[].name", "--output", "json")
            return set(json.loads(out or "[]"))
        except RuntimeError as exc:
            if attempt == attempts:
                raise SystemExit(f"Could not list {container} in {account}: {exc}") from exc
            print(f"  waiting for Blob access ({str(exc).splitlines()[-1]}); retrying in {delay}s [{attempt}/{attempts}]")
            time.sleep(delay)
    return set()


def main() -> int:
    try:
        account = os.environ["AZURE_STORAGE_ACCOUNT_NAME"]
    except KeyError as exc:
        raise SystemExit(f"Missing azd environment value {exc}. Run 'azd provision' first.") from exc
    container = os.environ.get("POLICIES_CONTAINER", "policies")

    existing = existing_policies(account, container)
    for path in sorted(POLICIES_DIR.glob("*.md")):
        if path.name in existing:
            print(f"kept     {container}/{path.name}")
            continue
        az("storage", "blob", "upload", "--account-name", account, "--container-name", container,
           "--name", path.name, "--file", str(path), "--content-type", "text/markdown",
           "--auth-mode", "login", "--output", "none")
        print(f"uploaded {container}/{path.name}")
    print("Policies are ready. Send requests with: python scripts/demo.py send")
    return 0


if __name__ == "__main__":
    sys.exit(main())
