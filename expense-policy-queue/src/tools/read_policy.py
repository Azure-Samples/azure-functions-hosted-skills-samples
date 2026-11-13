"""Reads one expense policy document from the ``policies`` Blob container."""

from typing import Any

import _storage


def read_policy(name: str) -> dict[str, Any]:
    """Read the full Markdown of one expense policy document.

    Args:
        name: Document name returned by list_policies, for example ``travel-policy.md``.
    """
    try:
        return _storage.read_policy_document(name)
    except (ValueError, LookupError) as exc:
        return {"error": str(exc), "name": name}
