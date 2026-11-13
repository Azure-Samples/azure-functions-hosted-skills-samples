# Copyright (c) Microsoft Corporation.
# Licensed under the MIT License.

"""Returns the record schema that the ingest function enforces.

The schema is bundled with the app (``src/record-schema.json``), so the hosted skill always
repairs against exactly what ``ingest`` validates.
"""

from typing import Any

from ingestion import records


def get_record_schema() -> dict[str, Any]:
    """Return the JSON Schema (draft 2020-12) that every ingested record must match."""
    return records.schema()
