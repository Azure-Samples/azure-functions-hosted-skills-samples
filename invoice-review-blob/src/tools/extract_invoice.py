# Copyright (c) Microsoft Corporation.
# Licensed under the MIT License.

"""Reads an uploaded invoice file and returns its fields or text.

PDFs are read with Azure AI Document Intelligence's prebuilt invoice model when
``DOCUMENT_INTELLIGENCE_ENDPOINT`` is set, and with pypdf text extraction otherwise.
Text and JSON files are returned as-is, which is handy for invoices exported from
another system.
"""

from __future__ import annotations

import io
import json
import os
from pathlib import PurePosixPath
from typing import Any

import _storage

MAX_TEXT_CHARS = 20_000


def _pdf_text(data: bytes) -> str:
    from pypdf import PdfReader

    reader = PdfReader(io.BytesIO(data))
    return "\n".join(page.extract_text() or "" for page in reader.pages)


def _field_value(field: Any) -> Any:
    if field is None:
        return None
    if getattr(field, "value_array", None):
        return [_field_value(item) for item in field.value_array]
    if getattr(field, "value_object", None):
        return {key: _field_value(value) for key, value in field.value_object.items()}
    return field.content


def _document_intelligence(data: bytes, endpoint: str) -> dict[str, Any]:
    from azure.ai.documentintelligence import DocumentIntelligenceClient
    from azure.ai.documentintelligence.models import AnalyzeDocumentRequest
    from azure.identity import DefaultAzureCredential

    credential = DefaultAzureCredential(managed_identity_client_id=os.environ.get("AZURE_CLIENT_ID"))
    client = DocumentIntelligenceClient(endpoint, credential)
    result = client.begin_analyze_document(
        "prebuilt-invoice", AnalyzeDocumentRequest(bytes_source=data)
    ).result()
    fields = result.documents[0].fields if result.documents else {}
    return {
        "fields": {name: _field_value(field) for name, field in (fields or {}).items()},
        "text": (result.content or "")[:MAX_TEXT_CHARS],
    }


def extract_invoice(blob_name: str) -> dict[str, Any]:
    """Read an invoice file from the invoices container and return its fields or text.

    Args:
        blob_name: The blob ``name`` from the trigger data, for example ``invoices/INV-FAB-1001.pdf``.
    """
    try:
        path = _storage.invoice_path(blob_name)
        data = _storage.read_invoice(path)
    except Exception as exc:  # noqa: BLE001 - report the problem to the model instead of failing the run
        return {"error": f"Could not read invoice {blob_name!r}: {exc}"}

    suffix = PurePosixPath(path).suffix.lower()
    result: dict[str, Any] = {"blob_name": path, "size_bytes": len(data)}
    try:
        if suffix == ".pdf":
            endpoint = os.environ.get("DOCUMENT_INTELLIGENCE_ENDPOINT", "")
            if endpoint:
                result["extraction_method"] = "document_intelligence_prebuilt_invoice"
                result.update(_document_intelligence(data, endpoint))
            else:
                result["extraction_method"] = "pdf_text"
                result["text"] = _pdf_text(data)[:MAX_TEXT_CHARS]
        elif suffix == ".json":
            result["extraction_method"] = "json"
            result["fields"] = json.loads(data)
        elif suffix in (".txt", ".md"):
            result["extraction_method"] = "text"
            result["text"] = data.decode("utf-8", errors="replace")[:MAX_TEXT_CHARS]
        else:
            return {**result, "error": f"Unsupported file type {suffix or '(none)'}; upload PDF, JSON, or text."}
    except Exception as exc:  # noqa: BLE001
        return {**result, "error": f"Could not extract invoice content: {exc}"}

    if not result.get("text") and not result.get("fields"):
        result["error"] = "The file has no readable text. Scanned PDFs need Document Intelligence."
    return result
