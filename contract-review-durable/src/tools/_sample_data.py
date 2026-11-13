"""Shared loader for the sample contract playbook in ``src/data``.

Replace these lookups with your contract lifecycle management (CLM) system or the
legal team's clause library.
"""

import json
from functools import cache
from pathlib import Path
from typing import Any

DATA_DIR = Path(__file__).resolve().parent.parent / "data"


@cache
def load(name: str) -> dict[str, Any]:
    with (DATA_DIR / f"{name}.json").open(encoding="utf-8") as f:
        return json.load(f)
