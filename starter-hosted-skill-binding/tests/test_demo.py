# Copyright (c) Microsoft Corporation.
# Licensed under the MIT License.

import importlib.util
from pathlib import Path

import pytest

spec = importlib.util.spec_from_file_location(
    "binding_starter_demo", Path(__file__).resolve().parent.parent / "scripts" / "demo.py",
)
demo = importlib.util.module_from_spec(spec)
spec.loader.exec_module(demo)


def test_load_samples_and_unknown_id():
    assert len(demo.load_samples()) == 2
    assert demo.load_samples("T-1001")[0]["ticket_id"] == "T-1001"
    with pytest.raises(SystemExit):
        demo.load_samples("missing")


def test_e2e_requires_every_submitted_ticket(monkeypatch):
    sent = []
    monkeypatch.setattr(demo, "azd_values", lambda: {"APPLICATIONINSIGHTS_RESOURCE_ID": "test"})
    monkeypatch.setattr(demo, "send", lambda messages, local: sent.extend(messages))
    monkeypatch.setattr(demo.time, "sleep", lambda seconds: None)
    ticks = iter([0, 0, 2])
    monkeypatch.setattr(demo.time, "monotonic", lambda: next(ticks))
    monkeypatch.setattr(demo, "completed_tickets", lambda resource, since, ids: {"unrelated-ticket"})
    assert demo.e2e(1) == 1
    assert len(sent) == 2
    assert all(message["ticket_id"] not in {"T-1001", "T-1002"} for message in sent)


def test_e2e_passes_for_correlated_completions(monkeypatch):
    monkeypatch.setattr(demo, "azd_values", lambda: {"APPLICATIONINSIGHTS_RESOURCE_ID": "test"})
    monkeypatch.setattr(demo, "send", lambda messages, local: None)
    monkeypatch.setattr(demo, "completed_tickets", lambda resource, since, ids: ids)
    assert demo.e2e(1) == 0