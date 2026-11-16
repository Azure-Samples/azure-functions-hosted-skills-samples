# Copyright (c) Microsoft Corporation.
# Licensed under the MIT License.

"""Exercise the indexed handler and real injection decorator, never a provider."""

import asyncio
import inspect
import json
from types import SimpleNamespace

import azure.functions as func
import pytest

import supplier_process as process


def invoke(invocation, update):
    body = update if isinstance(update, bytes) else json.dumps(update).encode("utf-8")
    return asyncio.run(invocation.handler(func.ServiceBusMessage(body=body)))


def record_for(update, assessment):
    return process.make_record(update, process.load_context(update), assessment, used_skill=True)


def test_real_app_indexes_only_service_bus_queue_trigger(indexed_app):
    assert isinstance(indexed_app.module.app, func.FunctionApp)
    assert indexed_app.function.get_function_name() == "review_supplier_change"
    bindings = json.loads(indexed_app.function.get_function_json())["bindings"]
    assert len(bindings) == 1  # No hostedSkill host binding or auto HTTP/chat/MCP endpoint.
    assert bindings[0]["type"] == "serviceBusTrigger"
    assert bindings[0]["direction"].lower() == "in"
    assert bindings[0]["name"] == "message"
    assert bindings[0]["queueName"] == "supplier-changes"
    assert bindings[0]["connection"] == "SERVICEBUS"
    assert list(inspect.signature(indexed_app.handler).parameters) == ["message"]


def test_routine_update_never_calls_model(invocation, sample):
    update = sample()
    assert invoke(invocation, update) is None
    invocation.run.assert_not_awaited()
    invocation.read.assert_called_once_with(update["change_id"])
    invocation.save.assert_called_once()
    record = invocation.save.call_args.args[0]
    assert record["decision"] == "accept_update"
    assert record["used_skill"] is False
    assert record["model_recommendation"] is None
    assert record["requires_human_approval"] is False
    invocation.notify.assert_called_once_with(record)


@pytest.mark.parametrize("change_id", ["SC-1002", "SC-1003"])
def test_exception_calls_model_with_real_context_but_policy_forces_review(invocation, sample, change_id):
    update = sample(change_id)
    invoke(invocation, update)
    invocation.run.assert_awaited_once()
    assert invocation.run.await_args.kwargs == {}
    prompt = json.loads(invocation.run.await_args.args[0])
    assert prompt == {"supplier_update": update, "purchasing_context": process.load_context(update)}
    record = invocation.save.call_args.args[0]
    assert record["used_skill"] is True
    assert record["model_recommendation"] == "accept_update"
    assert record["decision"] == "buyer_review"
    assert record["requires_human_approval"] is True
    assert record["policy_reasons"] == process.policy_reasons(update, process.load_context(update))
    invocation.notify.assert_called_once_with(record)


def test_exception_kind_alone_calls_model_and_preserves_clarification(invocation, sample, assessment):
    update = {**sample(), "change_kind": "exception"}
    assessment["recommendation"] = "request_clarification"
    invocation.run.return_value = SimpleNamespace(content=f"```json\n{json.dumps(assessment)}\n```")
    invoke(invocation, update)
    invocation.run.assert_awaited_once()
    record = invocation.save.call_args.args[0]
    assert record["policy_reasons"] == []
    assert record["decision"] == "request_clarification"
    assert record["requires_human_approval"] is True


@pytest.mark.parametrize("body", [b"\xff", b"{", b"", b"null", b"[]", b'"hello"'], ids=["utf8", "json", "empty", "null", "array", "scalar"])
def test_malformed_bodies_fail_before_store_or_model(invocation, body):
    with pytest.raises(ValueError):
        invoke(invocation, body)
    invocation.run.assert_not_awaited()
    invocation.read.assert_not_called()
    invocation.save.assert_not_called()
    invocation.notify.assert_not_called()


@pytest.mark.parametrize("field,value", [
    ("change_id", "../bad"), ("supplier_id", None), ("po_id", ""),
    ("change_kind", "email"), ("proposed_delivery_date", "2026-02-30"),
    ("quantity_available", True), ("quantity_available", 1.0),
    ("substitute_sku", "bad part"), ("explanation", " "),
    ("explanation", "x" * 10_001), ("unknown", True),
], ids=["id", "supplier", "po", "kind", "date", "bool-quantity", "float-quantity", "sku", "blank", "long", "extra"])
def test_invalid_fields_fail_before_store_or_model(invocation, sample, field, value):
    with pytest.raises(ValueError):
        invoke(invocation, {**sample(), field: value})
    invocation.run.assert_not_awaited()
    invocation.read.assert_not_called()
    invocation.save.assert_not_called()
    invocation.notify.assert_not_called()


def test_missing_field_fails_before_store_or_model(invocation, sample):
    update = sample()
    del update["explanation"]
    with pytest.raises(ValueError, match="missing or unknown"):
        invoke(invocation, update)
    invocation.read.assert_not_called()
    invocation.run.assert_not_awaited()
    invocation.save.assert_not_called()
    invocation.notify.assert_not_called()


@pytest.mark.parametrize("field,value,error", [
    ("po_id", "PO-unknown", "Unknown purchase order"),
    ("supplier_id", "SUP-wrong", "Supplier does not match"),
])
def test_invalid_context_fails_without_fabricated_evidence(invocation, sample, field, value, error):
    with pytest.raises(ValueError, match=error):
        invoke(invocation, {**sample(), field: value})
    invocation.read.assert_called_once()
    invocation.run.assert_not_awaited()
    invocation.save.assert_not_called()
    invocation.notify.assert_not_called()


@pytest.mark.parametrize("kind", ["provider", "schema", "json"])
def test_skill_failures_propagate_without_persistence(invocation, sample, kind):
    from azure_functions_agents.response_contract import InvalidResponseJsonError, ResponseSchemaValidationError

    error = {
        "provider": RuntimeError("provider unavailable"),
        "schema": ResponseSchemaValidationError("recommendation is invalid"),
        "json": InvalidResponseJsonError("invalid JSON"),
    }[kind]
    invocation.run.side_effect = error
    with pytest.raises(type(error)) as raised:
        invoke(invocation, sample("SC-1002"))
    assert raised.value is error
    invocation.save.assert_not_called()
    invocation.notify.assert_not_called()


@pytest.mark.parametrize("content", ["not-json", "{}", '{"recommendation":"approve"}'], ids=["json", "missing", "invalid"])
def test_bad_model_content_does_not_persist(invocation, sample, content):
    invocation.run.return_value = SimpleNamespace(content=content)
    with pytest.raises(ValueError):
        invoke(invocation, sample("SC-1002"))
    invocation.save.assert_not_called()
    invocation.notify.assert_not_called()


def test_duplicate_uses_saved_record_without_model_or_context_lookup(invocation, sample, assessment, monkeypatch):
    update = sample("SC-1002")
    saved = record_for(update, assessment)
    invocation.read.return_value = saved
    monkeypatch.setattr(invocation.module.process, "load_context", lambda _: pytest.fail("replay must use stored evidence"))
    invoke(invocation, update)
    invoke(invocation, update)
    invocation.run.assert_not_awaited()
    invocation.save.assert_not_called()
    assert invocation.notify.call_count == 2
    assert all(call.args[0] is saved for call in invocation.notify.call_args_list)


def test_failed_notification_retry_reads_saved_decision(invocation, sample):
    update = sample("SC-1002")
    committed = {}
    events = []

    def read(change_id):
        events.append("read")
        return committed.get(change_id)

    def save(record):
        events.append("save")
        committed[record["change_id"]] = record
        return record

    def notify(record):
        events.append("notify")
        assert record is committed[update["change_id"]]
        if events.count("notify") == 1:
            raise RuntimeError("send failed")

    invocation.read.side_effect = read
    invocation.save.side_effect = save
    invocation.notify.side_effect = notify
    with pytest.raises(RuntimeError, match="send failed"):
        invoke(invocation, update)
    invoke(invocation, update)
    assert events == ["read", "save", "notify", "read", "notify"]
    invocation.run.assert_awaited_once()
    invocation.save.assert_called_once()
    assert invocation.notify.call_count == 2


@pytest.mark.parametrize("race", [False, True], ids=["existing", "concurrent"])
def test_id_collision_rejected_before_notification(invocation, sample, assessment, race):
    update = sample("SC-1002")
    saved = record_for({**update, "explanation": "A different payload."}, assessment)
    if race:
        invocation.save.side_effect = None
        invocation.save.return_value = saved
    else:
        invocation.read.return_value = saved
    with pytest.raises(ValueError, match="different input"):
        invoke(invocation, update)
    invocation.notify.assert_not_called()
    if not race:
        invocation.run.assert_not_awaited()
        invocation.save.assert_not_called()


def test_concurrent_save_returns_authoritative_existing_decision(invocation, sample, assessment):
    update = {**sample(), "change_kind": "exception"}
    saved = record_for(update, {**assessment, "recommendation": "buyer_review", "summary": "First committed decision."})
    invocation.save.side_effect = None
    invocation.save.return_value = saved
    invoke(invocation, update)
    proposed = invocation.save.call_args.args[0]
    assert proposed["decision"] == "accept_update"
    assert saved["decision"] == "buyer_review"
    invocation.notify.assert_called_once_with(saved)
    assert invocation.notify.call_args.args[0] is saved


@pytest.mark.parametrize("boundary", ["read", "save"])
def test_storage_failure_propagates_without_notification(invocation, sample, boundary):
    error = RuntimeError("storage unavailable")
    getattr(invocation, boundary).side_effect = error
    with pytest.raises(RuntimeError) as raised:
        invoke(invocation, sample())
    assert raised.value is error
    invocation.notify.assert_not_called()
    invocation.run.assert_not_awaited()
    if boundary == "read":
        invocation.save.assert_not_called()