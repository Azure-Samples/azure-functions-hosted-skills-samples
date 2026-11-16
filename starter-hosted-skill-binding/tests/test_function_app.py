# Copyright (c) Microsoft Corporation.
# Licensed under the MIT License.

import asyncio
import inspect
import json
import logging
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import azure.functions as func
import pytest
import yaml
from azure_functions_agents import HostedSkill
from azure_functions_agents.response_contract import (
    ResponseSchemaValidationError,
    validate_response_contract,
)

TEMPLATE_DIR = Path(__file__).resolve().parent.parent
TRIAGE = {
    "category": "billing",
    "urgency": "high",
    "sentiment": "negative",
    "summary": "The customer reports a duplicate charge.",
    "suggested_reply": "Thanks for letting us know. We will investigate.",
}
TICKET = {
    "ticket_id": "T-1001",
    "customer": "private@example.com",
    "subject": "Charged twice",
    "message": "My card was charged twice. Please help.",
}


def invoke(function, body):
    message = func.QueueMessage(body=body)
    asyncio.run(function.get_user_function()(message=message))


def test_indexes_queue_without_host_binding_for_skill(function):
    metadata = json.loads(function.get_function_json())
    assert function.get_function_name() == "triage_support_message"
    bindings = metadata["bindings"]
    assert len(bindings) == 1
    assert bindings[0]["direction"].lower() == "in"
    assert {key: value for key, value in bindings[0].items() if key != "direction"} == {
        "type": "queueTrigger", "name": "message",
        "queueName": "support-requests", "connection": "SUPPORT_QUEUE",
    }
    assert "skill" not in inspect.signature(function.get_user_function()).parameters


def test_calls_skill_and_logs_correlated_classification(function, monkeypatch, caplog):
    run = AsyncMock(return_value=SimpleNamespace(content=json.dumps(TRIAGE)))
    monkeypatch.setattr(HostedSkill, "run", run)
    with caplog.at_level(logging.INFO):
        invoke(function, json.dumps(TICKET).encode())
    run.assert_awaited_once()
    prompt = json.loads(run.await_args.args[0])
    assert prompt == {key: TICKET[key] for key in ("subject", "message")}
    messages = [record.getMessage() for record in caplog.records]
    completion = next(message for message in messages if message.startswith("Support triage completed: "))
    assert json.loads(completion.split(": ", 1)[1]) == {
        "ticket_id": "T-1001", "category": "billing", "urgency": "high", "sentiment": "negative",
    }
    assert TRIAGE["summary"] not in caplog.text
    assert TRIAGE["suggested_reply"] not in caplog.text
    assert TICKET["customer"] not in caplog.text


def test_full_content_logging_is_opt_in(function, monkeypatch, caplog):
    monkeypatch.setenv("ENABLE_SENSITIVE_DATA", "true")
    monkeypatch.setattr(HostedSkill, "run", AsyncMock(return_value=SimpleNamespace(content=json.dumps(TRIAGE))))
    with caplog.at_level(logging.INFO):
        invoke(function, json.dumps(TICKET).encode())
    assert TRIAGE["summary"] in caplog.text
    assert '"ticket_id": "T-1001"' in caplog.text


@pytest.mark.parametrize("body", [
    b"not json", b"\xff", b"[]", b"null", b"{}",
    json.dumps({**TICKET, "ticket_id": "../unsafe"}).encode(),
    json.dumps({**TICKET, "ticket_id": 123}).encode(),
    json.dumps({**TICKET, "subject": " "}).encode(),
    json.dumps({**TICKET, "message": None}).encode(),
    json.dumps({**TICKET, "message": "x" * 10_001}).encode(),
], ids=["invalid-json", "invalid-utf8", "array", "null", "missing-fields", "unsafe-id", "numeric-id", "blank-subject", "null-message", "oversized-message"])
def test_invalid_messages_fail_before_model_call(function, monkeypatch, body):
    run = AsyncMock()
    monkeypatch.setattr(HostedSkill, "run", run)
    with pytest.raises(ValueError):
        invoke(function, body)
    run.assert_not_awaited()


@pytest.mark.parametrize("error", [RuntimeError("Provider unavailable"), ResponseSchemaValidationError("bad output")])
def test_failures_propagate_for_queue_retry(function, monkeypatch, caplog, error):
    monkeypatch.setattr(HostedSkill, "run", AsyncMock(side_effect=error))
    with caplog.at_level(logging.INFO), pytest.raises(type(error)):
        invoke(function, json.dumps(TICKET).encode())
    assert "Support triage completed:" not in caplog.text


def test_agent_is_endpointless_with_enforced_response_schema():
    text = (TEMPLATE_DIR / "src" / "support_triage.agent.md").read_text(encoding="utf-8")
    config = yaml.safe_load(text.split("---", 2)[1])
    assert "trigger" not in config
    assert "builtin_endpoints" not in config
    assert config["tools"] is False
    assert config["mcp"] is False
    assert validate_response_contract(json.dumps(TRIAGE), config["response_schema"]) == TRIAGE
    with pytest.raises(ResponseSchemaValidationError):
        validate_response_contract(json.dumps({**TRIAGE, "urgency": "critical"}), config["response_schema"])


@pytest.mark.parametrize("sample", ["T-1001", "T-1002"])
def test_shipped_samples_invoke_skill(function, monkeypatch, sample):
    run = AsyncMock(return_value=SimpleNamespace(content=json.dumps(TRIAGE)))
    monkeypatch.setattr(HostedSkill, "run", run)
    invoke(function, (TEMPLATE_DIR / "samples" / f"{sample}.json").read_bytes())
    run.assert_awaited_once()