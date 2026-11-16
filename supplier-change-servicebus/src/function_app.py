# Copyright (c) Microsoft Corporation.
# Licensed under the MIT License.

"""The application owns the process; the Hosted Skill supplies one judgment step."""

import asyncio
import json
import logging

import azure.functions as func
from azure_functions_agents import HostedSkill, HostedSkillFunctionApp
from azure_functions_agents.response_contract import extract_json_from_response

import supplier_process as process
import supplier_store as store

app = HostedSkillFunctionApp()


@app.function_name("review_supplier_change")
@app.service_bus_queue_trigger(arg_name="message", queue_name="supplier-changes", connection="SERVICEBUS")
@app.hosted_skill(arg_name="skill", agent_name="supplier_assessment")
async def review_supplier_change(message: func.ServiceBusMessage, skill: HostedSkill) -> None:
    try:
        raw = json.loads(message.get_body().decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        raise ValueError("Supplier message must be UTF-8 JSON") from None
    update = process.validate_update(raw)
    record = await asyncio.to_thread(store.read, update["change_id"])
    if record is None:
        context = process.load_context(update)
        assessment = process.routine_assessment(update, context)
        used_skill = assessment is None
        if used_skill:
            result = await skill.run(process.build_prompt(update, context))
            assessment = json.loads(extract_json_from_response(result.content))
        proposed = process.make_record(update, context, assessment, used_skill=used_skill)
        record = await asyncio.to_thread(store.save_once, proposed)
    process.verify_replay(record, update)
    # Notification errors propagate to the host. A retry reuses the saved decision.
    await asyncio.to_thread(store.notify_buyer, record)
    logging.info(
        "Supplier change completed: change_id=%s decision=%s used_skill=%s requires_human_approval=%s",
        record["change_id"], record["decision"], record["used_skill"], record["requires_human_approval"],
    )