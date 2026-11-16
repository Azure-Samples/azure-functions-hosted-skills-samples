# Copyright (c) Microsoft Corporation.
# Licensed under the MIT License.

"""An ordinary queue-triggered function that explicitly invokes a Hosted Skill."""

import json
import logging
import os
import re

import azure.functions as func
from azure_functions_agents import HostedSkill, HostedSkillFunctionApp
from azure_functions_agents.response_contract import extract_json_from_response

app = HostedSkillFunctionApp()


@app.function_name("triage_support_message")
@app.queue_trigger(arg_name="message", queue_name="support-requests", connection="SUPPORT_QUEUE")
@app.hosted_skill(arg_name="skill", agent_name="support_triage")
async def triage_support_message(message: func.QueueMessage, skill: HostedSkill) -> None:
    # Validate in code before spending a model call. Invalid messages follow the
    # queue's normal retry/poison behavior; never log their raw contents.
    try:
        ticket = json.loads(message.get_body().decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        raise ValueError("Queue message must be a UTF-8 JSON object") from None
    if not isinstance(ticket, dict):
        raise ValueError("Queue message must be a JSON object")
    ticket_id = ticket.get("ticket_id")
    if not isinstance(ticket_id, str) or re.fullmatch(r"[A-Za-z0-9_-]{1,80}", ticket_id) is None:
        raise ValueError("ticket_id must contain 1-80 letters, digits, hyphens, or underscores")
    for field in ("subject", "message"):
        value = ticket.get(field)
        if not isinstance(value, str) or not value.strip() or len(value) > 10_000:
            raise ValueError(f"{field} must be a nonblank string of at most 10000 characters")

    # Pass only what the skill needs, not the customer's address or queue metadata.
    prompt = json.dumps({"subject": ticket["subject"], "message": ticket["message"]})
    result = await skill.run(prompt)
    triage = json.loads(extract_json_from_response(result.content))

    # The runtime enforces the agent's response_schema before run() returns.
    # Keep correlation application-owned rather than asking the model to echo IDs.
    completion = {
        "ticket_id": ticket_id,
        "category": triage["category"],
        "urgency": triage["urgency"],
        "sentiment": triage["sentiment"],
    }
    logging.info("Support triage completed: %s", json.dumps(completion))
    if os.environ.get("ENABLE_SENSITIVE_DATA", "").lower() == "true":
        logging.info("Support triage content: %s", json.dumps({**triage, "ticket_id": ticket_id}))