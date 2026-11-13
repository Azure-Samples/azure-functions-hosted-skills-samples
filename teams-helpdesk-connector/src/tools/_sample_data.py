# Copyright (c) Microsoft Corporation.
# Licensed under the MIT License.

"""Shared loader for the sample helpdesk data in ``src/data``.

Replace these lookups with your knowledge base (for example a SharePoint site or
Azure AI Search index) and your IT service management catalog.
"""

from __future__ import annotations

import json
import re
from functools import cache
from pathlib import Path
from typing import Any

DATA_DIR = Path(__file__).resolve().parent.parent / "data"
_WORD = re.compile(r"[a-z0-9][a-z0-9+#.-]*")


@cache
def load(name: str) -> dict[str, Any]:
    with (DATA_DIR / f"{name}.json").open(encoding="utf-8") as f:
        return json.load(f)


def words(text: str) -> set[str]:
    return set(_WORD.findall(text.lower()))
