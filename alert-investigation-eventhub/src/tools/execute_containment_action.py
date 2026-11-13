"""Containment guardrail: the hosted skill can never execute a containment action.

Isolating hosts, disabling accounts, and blocking traffic are destructive. This tool
exists so that a model that tries to act gets a clear, code-enforced refusal and is
told to recommend the action instead. A person approves and runs containment in your
EDR or identity platform.
"""

from __future__ import annotations

from typing import Any


def execute_containment_action(action: str, target: str) -> dict[str, Any]:
    """Attempt a containment action such as isolating a host or disabling an account. Always refused.

    Containment is never executed by this hosted skill. Add the action to
    ``recommended_containment`` in ``record_investigation`` so an analyst can approve it.

    Args:
        action: The action, for example ``isolate_host`` or ``disable_account``.
        target: The host, user, or indicator the action applies to.
    """
    return {
        "status": "refused",
        "executed": False,
        "action": action,
        "target": target,
        "reason": (
            "Containment actions are never executed automatically. Add this action to "
            "recommended_containment in record_investigation; an analyst approves and runs it."
        ),
    }
