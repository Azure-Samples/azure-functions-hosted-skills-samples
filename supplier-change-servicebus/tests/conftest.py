# Copyright (c) Microsoft Corporation.
# Licensed under the MIT License.

"""Offline fixtures; index the real app without opening any Azure clients."""

import importlib.util
import json
import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
# Make pure process/store imports available during test collection as well.
sys.path.insert(0, str(SRC))


@pytest.fixture
def sample():
    def load(change_id="SC-1001"):
        return json.loads((ROOT / "samples" / f"{change_id}.json").read_text(encoding="utf-8"))

    return load


@pytest.fixture
def assessment():
    return {
        "recommendation": "accept_update",
        "summary": "The supplied evidence supports this update.",
        "reasons": ["Review the purchasing evidence."],
        "questions": [],
    }


@pytest.fixture
def response_schema():
    import yaml

    text = (SRC / "supplier_assessment.agent.md").read_text(encoding="utf-8")
    return yaml.safe_load(text.split("---", 2)[1])["response_schema"]


@pytest.fixture
def indexed_app(monkeypatch):
    monkeypatch.syspath_prepend(str(SRC))
    monkeypatch.chdir(SRC)
    monkeypatch.setenv("AZURE_FUNCTIONS_AGENTS_APP_ROOT", str(SRC))
    monkeypatch.setenv("AZURE_FUNCTIONS_AGENTS_PROVIDER", "foundry")
    monkeypatch.setenv("FOUNDRY_PROJECT_ENDPOINT", "https://example.services.ai.azure.com/api/projects/test")
    monkeypatch.setenv("FOUNDRY_MODEL", "test-model")
    monkeypatch.delenv("AZURE_FUNCTIONS_AGENTS_ENABLE_COPILOT", raising=False)
    monkeypatch.delenv("ENABLE_SENSITIVE_DATA", raising=False)
    spec = importlib.util.spec_from_file_location("supplier_change_app_under_test", SRC / "function_app.py")
    module = importlib.util.module_from_spec(spec)
    monkeypatch.setitem(sys.modules, spec.name, module)
    spec.loader.exec_module(module)
    functions = module.app.get_functions()
    assert len(functions) == 1
    return SimpleNamespace(module=module, function=functions[0], handler=functions[0].get_user_function())


@pytest.fixture
def invocation(indexed_app, monkeypatch, assessment):
    from azure_functions_agents import HostedSkill

    run = AsyncMock(return_value=SimpleNamespace(content=json.dumps(assessment)))
    monkeypatch.setattr(HostedSkill, "run", run)
    read = Mock(return_value=None)
    save = Mock(side_effect=lambda record: record)
    notify = Mock()
    monkeypatch.setattr(indexed_app.module.store, "read", read)
    monkeypatch.setattr(indexed_app.module.store, "save_once", save)
    monkeypatch.setattr(indexed_app.module.store, "notify_buyer", notify)
    return SimpleNamespace(
        handler=indexed_app.handler, module=indexed_app.module,
        run=run, read=read, save=save, notify=notify,
    )