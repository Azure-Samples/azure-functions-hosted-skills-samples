# Copyright (c) Microsoft Corporation.
# Licensed under the MIT License.

"""Offline demo checks: no credentials, model calls, or Azure resources needed."""

import copy
import importlib.util
import json
import sys
import types
from contextlib import contextmanager
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]


def _load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


demo = _load("supplier_demo_under_test", ROOT / "scripts" / "demo.py")
process = _load("supplier_process_for_demo_test", ROOT / "src" / "supplier_process.py")


def _records(messages):
    records = {}
    for message in messages:
        context = process.load_context(message)
        assessment = process.routine_assessment(message, context)
        used_skill = assessment is None
        if assessment is None:
            # Deliberately optimistic model output: code must still force review.
            assessment = {
                "recommendation": "accept_update", "summary": "Illustrative model assessment.",
                "reasons": ["Review the supplied purchasing evidence."], "questions": [],
            }
        records[message["change_id"]] = process.make_record(
            message, context, assessment, used_skill=used_skill,
        )
    return records


def test_actual_samples_parse_and_match_deterministic_contract():
    messages = demo.load_samples()
    assert [message["change_id"] for message in messages] == ["SC-1001", "SC-1002", "SC-1003"]
    for message in messages:
        assert process.validate_update(message) == message
    records = _records(messages)
    assert demo.validate(records, messages) == []
    assert [record["decision"] for record in records.values()] == ["accept_update", "buyer_review", "buyer_review"]
    assert [record["used_skill"] for record in records.values()] == [False, True, True]
    assert [record["requires_human_approval"] for record in records.values()] == [False, True, True]
    assert records["SC-1002"]["facts"]["shortfall_units"] == 120
    assert records["SC-1003"]["model_recommendation"] == "accept_update"


def test_sample_filter_and_unknown_id():
    assert [sample["change_id"] for sample in demo.load_samples("SC-1002")] == ["SC-1002"]
    with pytest.raises(ValueError, match="No sample found"):
        demo.load_samples("SC-9999")


def test_unique_samples_preserve_payload_and_use_new_ids():
    originals = demo.load_samples()
    first, second = demo.unique_samples(), demo.unique_samples()
    assert {m["change_id"] for m in first}.isdisjoint(m["change_id"] for m in second)
    suffixes = set()
    for original, message in zip(originals, first, strict=True):
        assert message["change_id"].startswith(original["change_id"] + "-")
        suffixes.add(message["change_id"][len(original["change_id"]) + 1:])
        assert {**message, "change_id": original["change_id"]} == original
        process.validate_update(message)
    assert len(suffixes) == 1
    assert demo.validate(_records(first), first) == []


def test_missing_result_is_not_satisfied_by_unrelated_blob():
    messages = demo.unique_samples()
    records = _records(demo.load_samples())
    assert demo.validate(records, messages) == [
        f"{message['change_id']}: no recommendation recorded" for message in messages
    ]


@pytest.mark.parametrize("field,value,error", [
    ("decision", "request_clarification", "expected decision buyer_review"),
    ("used_skill", False, "expected used_skill=True"),
    ("used_skill", 1, "expected used_skill=True"),
    ("requires_human_approval", False, "incorrect requires_human_approval"),
    ("requires_human_approval", 1, "incorrect requires_human_approval"),
    ("change_id", "SC-unrelated", "change_id does not match input"),
    ("supplier_id", "SUP-unrelated", "supplier_id does not match input"),
    ("po_id", "PO-unrelated", "po_id does not match input"),
    ("input_fingerprint", "unrelated", "input_fingerprint does not match input"),
    ("reasons", [], "no valid reasons"),
    ("summary", " ", "no summary"),
    ("policy_reasons", [], "incorrect policy_reasons"),
    ("facts", {}, "incorrect computed facts"),
    ("context", {}, "incorrect purchasing evidence snapshot"),
    ("model_recommendation", "approve_substitute", "incorrect model_recommendation"),
])
def test_validation_rejects_wrong_result(field, value, error):
    messages = demo.load_samples("SC-1002")
    records = _records(messages)
    records["SC-1002"][field] = value
    assert any(error in failure for failure in demo.validate(records, messages))


def test_fast_path_requires_false_skill_flag_and_no_approval():
    messages = demo.load_samples("SC-1001")
    records = _records(messages)
    records["SC-1001"]["used_skill"] = True
    records["SC-1001"]["requires_human_approval"] = True
    failures = demo.validate(records, messages)
    assert "SC-1001: expected used_skill=False" in failures
    assert "SC-1001: incorrect requires_human_approval" in failures


def test_changed_input_with_same_id_does_not_match_old_record():
    messages = demo.load_samples("SC-1001")
    records = _records(messages)
    changed = copy.deepcopy(messages)
    changed[0]["explanation"] += " Changed payload."
    assert "SC-1001: input_fingerprint does not match input" in demo.validate(records, changed)


@pytest.mark.parametrize("value", ["", "<your-namespace>.servicebus.windows.net", "example.servicebus.windows.net", "UseDevelopmentStorage=true", "sb://real-supplier.servicebus.windows.net", "real-supplier"])
def test_send_rejects_missing_placeholder_or_invalid_namespace(value):
    target = demo.Target(servicebus_namespace=value, review_namespace="real-supplier")
    with pytest.raises(ValueError, match="real Service Bus"):
        demo.send(target, [])


def test_send_rejects_missing_output_namespace_and_namespace_mismatch():
    target = demo.Target(servicebus_namespace="real-supplier.servicebus.windows.net")
    with pytest.raises(ValueError, match="real Service Bus"):
        demo.validate_send_target(target)
    target.review_namespace = "other-supplier.servicebus.windows.net"
    with pytest.raises(ValueError, match="must match"):
        demo.validate_send_target(target)
    target.review_namespace = "real-supplier"
    with pytest.raises(ValueError, match="real Service Bus"):
        demo.validate_send_target(target)
    target.review_namespace = "real-supplier.servicebus.windows.net"
    assert demo.validate_send_target(target) == "real-supplier.servicebus.windows.net"


def test_local_fallback_reads_template_without_requiring_namespace(tmp_path, monkeypatch):
    (tmp_path / "src").mkdir()
    values = {"AzureWebJobsStorage": demo.AZURITE, "RECOMMENDATIONS_CONTAINER": "supplier-change-recommendations"}
    template = tmp_path / "src" / "local.settings.json.sample"
    template.write_text(json.dumps({"Values": values}), encoding="utf-8")
    monkeypatch.setattr(demo, "TEMPLATE_DIR", tmp_path)
    target = demo.resolve_target(True)
    assert target.local and target.storage_connection == demo.AZURITE
    assert target.servicebus_namespace == ""
    values["SERVICEBUS__fullyQualifiedNamespace"] = "real-supplier.servicebus.windows.net"
    values["SUPPLIER_SERVICEBUS_NAMESPACE"] = "real-supplier.servicebus.windows.net"
    (tmp_path / "src" / "local.settings.json").write_text(json.dumps({"Values": values}), encoding="utf-8")
    assert demo.validate_send_target(demo.resolve_target(True)) == "real-supplier.servicebus.windows.net"


def test_deployed_outputs(monkeypatch):
    monkeypatch.setattr(demo, "_azd_values", lambda: {
        "SERVICEBUS_FULLY_QUALIFIED_NAMESPACE": "real-supplier.servicebus.windows.net",
        "SERVICEBUS_QUEUE_NAME": "supplier-changes", "SERVICEBUS_REVIEW_QUEUE_NAME": "supplier-reviews",
        "RECOMMENDATIONS_CONTAINER": "supplier-change-recommendations", "AZURE_STORAGE_ACCOUNT_NAME": "supplierstore",
    })
    target = demo.resolve_target(False)
    assert target.storage_account == "supplierstore"
    assert target.storage_connection == ""
    assert target.container == "supplier-change-recommendations"
    assert demo.validate_send_target(target) == "real-supplier.servicebus.windows.net"


def test_read_results_downloads_only_exact_ids_without_namespace(monkeypatch):
    class NotFound(Exception):
        pass

    # Stub just the exception import so this test does not need Azure SDK packages.
    for name in ("azure", "azure.core", "azure.core.exceptions"):
        monkeypatch.setitem(sys.modules, name, types.ModuleType(name))
    monkeypatch.setattr(sys.modules["azure.core.exceptions"], "ResourceNotFoundError", NotFound, raising=False)
    downloaded = []

    class Container:
        def download_blob(self, name):
            downloaded.append(name)
            if name == "SC-1002-run.json":
                raise NotFound()
            return types.SimpleNamespace(readall=lambda: b'{"change_id":"SC-1001-run"}')

    @contextmanager
    def container(target):
        assert not target.servicebus_namespace
        yield Container()

    monkeypatch.setattr(demo, "_container_client", container)
    results = demo.read_results(demo.Target(local=True), ["SC-1001-run", "SC-1002-run"])
    assert downloaded == ["SC-1001-run.json", "SC-1002-run.json"]
    assert list(results) == ["SC-1001-run"]


def test_wait_polls_exact_ids_not_result_count(monkeypatch):
    calls = []

    def read(target, ids):
        calls.append(list(ids))
        return {"unrelated": {}} if len(calls) == 1 else {"SC-1001-run": {}}

    monkeypatch.setattr(demo, "read_results", read)
    monkeypatch.setattr(demo.time, "sleep", lambda seconds: None)
    assert demo.wait_for(demo.Target(), ["SC-1001-run"], 10) == {"SC-1001-run": {}}
    assert calls == [["SC-1001-run"], ["SC-1001-run"]]


def test_results_reports_missing_ids_without_namespace(monkeypatch, capsys):
    monkeypatch.setattr(demo, "resolve_target", lambda local: demo.Target(local=local))
    monkeypatch.setattr(demo, "read_results", lambda target, ids: {})
    assert demo.main(["results", "--local", "--id", "SC-1001-run", "--wait", "0"]) == 1
    assert "Missing recommendations: SC-1001-run" in capsys.readouterr().err


def test_e2e_failure_returns_nonzero(monkeypatch, capsys):
    monkeypatch.setattr(demo, "resolve_target", lambda local: demo.Target(local=local))
    monkeypatch.setattr(demo, "send", lambda target, messages: None)
    monkeypatch.setattr(demo, "wait_for", lambda target, ids, timeout: {})
    assert demo.main(["e2e", "--local", "--timeout", "0"]) == 1
    assert "no recommendation recorded" in capsys.readouterr().err