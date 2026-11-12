"""Shared loader for the sample business data in ``src/data``.

Files whose names start with ``_`` are not registered as tools, so the tool
modules in this folder can import this helper. Replace these lookups with calls
to your order, policy, and CRM systems.
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
