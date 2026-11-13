"""Returns password reset and account unlock guidance.

The guidance is fixed in code so the skill always points employees to self-service
password reset and never asks for, accepts, or sets a password itself.

Do not add ``from __future__ import annotations`` here: the runtime loads tool
files dynamically and pydantic must resolve the model's types at import time.
"""

from typing import Any, Literal

from pydantic import BaseModel, Field

SSPR_URL = "https://aka.ms/sspr"
SECURITY_INFO_URL = "https://mysignins.microsoft.com/security-info"


class PasswordHelpRequest(BaseModel):
    situation: Literal["forgot", "expired", "locked_out", "no_registered_method"] = Field(
        description="forgot = forgot the password; expired = password expired; locked_out = account locked after failed attempts; no_registered_method = cannot use self-service because no phone or Authenticator is registered."
    )


def get_password_reset_guidance(request: PasswordHelpRequest) -> dict[str, Any]:
    """Get the approved password reset or account unlock steps. The helpdesk never asks for or sets passwords."""
    steps = {
        "forgot": [
            f"Go to {SSPR_URL} and enter your work email address.",
            "Verify with Microsoft Authenticator or your registered phone.",
            "Choose a new password, then sign in again on your laptop and phone.",
        ],
        "expired": [
            "On a company laptop, press Ctrl+Alt+Del and choose 'Change a password'.",
            f"Anywhere else, go to {SSPR_URL} and follow the prompts.",
            "Update the password on your phone's mail and Teams apps when prompted.",
        ],
        "locked_out": [
            "Wait 15 minutes; the lock clears automatically.",
            f"To unlock now, use {SSPR_URL} and choose 'I forgot my password' to reset and unlock.",
        ],
        "no_registered_method": [
            "Self-service reset needs a registered method, so a helpdesk analyst must verify your identity by video call.",
            f"After you regain access, register a method at {SECURITY_INFO_URL}.",
        ],
    }[request.situation]
    return {
        "situation": request.situation,
        "steps": steps,
        "self_service_url": SSPR_URL,
        "needs_human": request.situation == "no_registered_method",
        "security_note": "IT never asks for your password. Do not post passwords or codes in Teams.",
        "source_url": "https://contoso.sharepoint.com/sites/it/kb/KB-1002",
    }
