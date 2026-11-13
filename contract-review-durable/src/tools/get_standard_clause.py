# Copyright (c) Microsoft Corporation.
# Licensed under the MIT License.

"""Approved standard clause language, used to draft suggested redlines."""

from typing import Any

from _sample_data import load


def get_standard_clause(clause: str) -> dict[str, Any]:
    """Get Contoso's approved standard language for one clause, to use as the suggested redline for a deviation.

    Args:
        clause: Playbook clause key, for example ``limitation_of_liability`` or ``governing_law``.
    """
    terms = load("standard_terms")
    key = clause.strip().lower()
    if key not in terms:
        return {"error": f"No standard language for clause {clause!r}.", "known_clauses": sorted(terms)}
    return {"clause": key, "standard_language": terms[key]}
