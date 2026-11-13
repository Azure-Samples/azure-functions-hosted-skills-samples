"""Contract review: a Durable Functions process that uses a hosted skill for the judgment step.

The hosted skill in ``contract_review.agent.md`` is registered by ``create_function_app()``.
The process around it is ordinary code: an HTTP starter, an orchestrator, and activities.
The orchestrator decides what happens next from the skill's structured output, so the
routing, approval wait, timeout, and audit record are deterministic and replayable.
"""

import json
import logging
import os
from typing import Any

import azure.durable_functions as df
import azure.functions as func
from azure_functions_agents import HostedSkill, create_function_app
from azure_functions_agents.response_contract import extract_json_from_response
from azurefunctions.extensions.http.fastapi import JSONResponse, Request

import contract_orchestration as orchestration
import contract_process as process
import contract_store as store

app = create_function_app()
bp = df.Blueprint()

ORCHESTRATOR = "contract_orchestrator"
APPROVAL_EVENT = orchestration.APPROVAL_EVENT
AWAITING_APPROVAL = orchestration.AWAITING_APPROVAL


def _base_url() -> str:
    # The worker sees the host's internal proxy address, so build public links from the
    # site host name (set in Azure and by Core Tools); fall back to relative links.
    host = os.environ.get("WEBSITE_HOSTNAME", "")
    if not host:
        return ""
    scheme = "http" if host.startswith(("localhost", "127.0.0.1")) else "https"
    return f"{scheme}://{host}"


def _instance_id_from_path(req: Request) -> str:
    # contracts/{instanceId} or contracts/{instanceId}/approve
    parts = [p for p in req.url.path.split("/") if p]
    index = parts.index("contracts") if "contracts" in parts else -1
    return parts[index + 1] if 0 <= index < len(parts) - 1 else ""


def _loads(value: str | None) -> Any:
    return json.loads(value) if value else None


# --- HTTP API -----------------------------------------------------------------------


@bp.function_name("start_contract_review")
@bp.route(route="contracts", methods=["POST"], auth_level=func.AuthLevel.FUNCTION)
@bp.durable_client_input(client_name="client")
async def start_contract_review(req: Request, client: df.DurableFunctionsClient) -> JSONResponse:
    """Start the review process for one contract (JSON body)."""
    try:
        contract = await req.json()
    except ValueError:
        return JSONResponse({"error": "The request body must be JSON."}, status_code=400)
    if not isinstance(contract, dict):
        return JSONResponse({"error": "The request body must be a JSON object."}, status_code=400)

    # Policy is captured once at start so every replay of this instance uses the same values.
    instance_id = await client.schedule_new_orchestration(
        ORCHESTRATOR, input={"contract": contract, "policy": process.policy_from_env()}
    )
    logging.info("Started contract review %s for %s", instance_id, contract.get("contract_id"))
    base = f"{_base_url()}/contracts/{instance_id}"
    return JSONResponse(
        {
            "instanceId": instance_id,
            "contractId": contract.get("contract_id"),
            "statusUrl": base,
            "approveUrl": f"{base}/approve",
        },
        status_code=202,
    )


@bp.function_name("get_contract_review")
@bp.route(route="contracts/{instanceId}", methods=["GET"], auth_level=func.AuthLevel.FUNCTION)
@bp.durable_client_input(client_name="client")
async def get_contract_review(req: Request, client: df.DurableFunctionsClient) -> JSONResponse:
    """Return the status, current stage, and (when finished) the decision for one review."""
    instance_id = _instance_id_from_path(req)
    state = await client.get_orchestration_state(instance_id, fetch_payloads=True) if instance_id else None
    if state is None:
        return JSONResponse({"error": f"No contract review {instance_id!r}."}, status_code=404)
    return JSONResponse(
        {
            "instanceId": state.instance_id,
            "runtimeStatus": state.runtime_status.name,
            "customStatus": _loads(state.serialized_custom_status),
            "output": _loads(state.serialized_output),
            "createdAt": state.created_at.isoformat(),
            "lastUpdatedAt": state.last_updated_at.isoformat(),
        }
    )


@bp.function_name("approve_contract")
@bp.route(route="contracts/{instanceId}/approve", methods=["POST"], auth_level=func.AuthLevel.FUNCTION)
@bp.durable_client_input(client_name="client")
async def approve_contract(req: Request, client: df.DurableFunctionsClient) -> JSONResponse:
    """Record a lawyer's decision: ``{"decision": "approved"|"rejected", "approver": "...", "comment": "..."}``."""
    instance_id = _instance_id_from_path(req)
    try:
        body = await req.json()
    except ValueError:
        body = None
    if not isinstance(body, dict) or body.get("decision") not in process.APPROVAL_DECISIONS or not body.get("approver"):
        return JSONResponse(
            {"error": "Send {\"decision\": \"approved\" or \"rejected\", \"approver\": \"...\", \"comment\": \"...\"}."},
            status_code=400,
        )

    state = await client.get_orchestration_state(instance_id, fetch_payloads=True) if instance_id else None
    if state is None:
        return JSONResponse({"error": f"No contract review {instance_id!r}."}, status_code=404)
    custom_status = _loads(state.serialized_custom_status) or {}
    if state.runtime_status.name != "RUNNING" or custom_status.get("stage") != AWAITING_APPROVAL:
        return JSONResponse(
            {
                "error": "This contract is not waiting for legal approval.",
                "runtimeStatus": state.runtime_status.name,
                "stage": custom_status.get("stage"),
            },
            status_code=409,
        )

    await client.raise_orchestration_event(instance_id, APPROVAL_EVENT, data=process.normalize_approval(body))
    return JSONResponse({"instanceId": instance_id, "event": APPROVAL_EVENT, "decision": body["decision"]}, status_code=202)


# --- Orchestrator --------------------------------------------------------------------


@bp.orchestration_trigger(context_name="context")
def contract_orchestrator(context: df.DurableOrchestrationContext):
    return (yield from orchestration.review_process(context))

# --- Activities ----------------------------------------------------------------------


@bp.activity_trigger(input_name="contract")
def validate_contract(contract: Any) -> dict:
    return process.validate_contract(contract)


@bp.activity_trigger(input_name="contract")
@app.hosted_skill(arg_name="skill", agent_name="contract_review")
async def review_contract(contract: dict, skill: HostedSkill) -> dict:
    # In-process call: no HTTP hop or function key. The runtime validates the response
    # against the skill's response_schema and raises if it does not match, so the
    # orchestrator's retry policy covers malformed model output.
    result = await skill.run(process.review_prompt(contract))
    review = json.loads(extract_json_from_response(result.content))
    return process.normalize_review(review, contract)


@bp.activity_trigger(input_name="request")
def create_legal_review_task(request: dict) -> str:
    contract = request["contract"]
    return store.write_json(
        store.task_path(request["instance_id"]),
        {
            "task_id": request["instance_id"],
            "status": "open",
            "contract_id": contract["contract_id"],
            "title": contract["title"],
            "counterparty": contract["counterparty"],
            "business_owner": contract.get("business_owner"),
            "annual_value": contract["annual_value"],
            "reasons": request["reasons"],
            "review": request["review"],
            "created_at": request["created_at"],
            "due_at": request["due_at"],
        },
    )


@bp.activity_trigger(input_name="decision")
def record_decision(decision: dict) -> str:
    instance_id = decision["instance_id"]
    task = store.read_json(store.task_path(instance_id))
    if task is not None and task.get("status") == "open":
        store.write_json(
            store.task_path(instance_id),
            {**task, "status": "closed", "outcome": decision["outcome"], "closed_at": decision["decided_at"]},
        )
    return store.write_json(store.decision_path(instance_id), decision)


app.register_functions(bp)
