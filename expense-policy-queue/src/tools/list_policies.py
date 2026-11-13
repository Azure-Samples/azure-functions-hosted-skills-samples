"""Lists the expense policy documents stored in the ``policies`` Blob container."""

from typing import Any

import _storage


def list_policies() -> dict[str, Any]:
    """List the expense policy documents in Blob Storage. Each entry has the document `name` and its `applies_to` scope line. Call this before choosing a policy."""
    try:
        documents = _storage.list_policy_documents()
    except Exception as exc:  # surface storage errors to the model instead of failing the run
        return {"error": f"Could not list policy documents: {type(exc).__name__}: {exc}"}
    if not documents:
        return {"policies": [], "error": "The policies container is empty. Seed it with scripts/demo.py seed."}
    return {"policies": documents, "fallback": "general-expense-policy.md"}
