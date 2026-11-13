"""Clause playbook lookup backed by sample data."""

from typing import Any

from _sample_data import load


def get_clause_playbook(contract_type: str) -> dict[str, Any]:
    """Get Contoso's legal playbook for a contract type: the required clauses and, for each, the standard position, the acceptable fallback, and the signals that make a clause non-standard.

    Args:
        contract_type: ``contract_type`` from the contract, for example ``saas_subscription`` or ``master_services``.
    """
    playbook = load("clause_playbook")
    key = contract_type.strip().lower()
    contract = playbook["contract_types"].get(key)
    if contract is None:
        return {
            "error": f"No playbook for contract type {contract_type!r}.",
            "known_contract_types": sorted(playbook["contract_types"]),
        }
    return {
        "contract_type": key,
        "title": contract["title"],
        "perspective": playbook["perspective"],
        "clauses": {name: playbook["clauses"][name] for name in contract["required_clauses"]},
    }
