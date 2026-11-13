"""Shared loader for the sample business data in ``src/data``.

Replace these lookups with calls to your product analytics, support desk, and
pricing systems.
"""

from __future__ import annotations

import json
from functools import cache
from pathlib import Path
from typing import Any

DATA_DIR = Path(__file__).resolve().parent.parent / "data"


@cache
def load(name: str) -> dict[str, Any]:
    with (DATA_DIR / f"{name}.json").open(encoding="utf-8") as f:
        return json.load(f)


def normalize_account_id(account_id: str) -> str:
    return account_id.strip().upper()