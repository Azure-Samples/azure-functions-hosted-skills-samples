# Copyright (c) Microsoft Corporation.
# Licensed under the MIT License.

import importlib.util
from pathlib import Path

import pytest

TEMPLATE_DIR = Path(__file__).resolve().parent.parent


@pytest.fixture
def function(monkeypatch):
    """Index the real app and decorator, without creating any model clients."""
    src = TEMPLATE_DIR / "src"
    monkeypatch.chdir(src)
    monkeypatch.setenv("AZURE_FUNCTIONS_AGENTS_APP_ROOT", str(src))
    monkeypatch.setenv("AZURE_FUNCTIONS_AGENTS_PROVIDER", "foundry")
    monkeypatch.setenv("FOUNDRY_PROJECT_ENDPOINT", "https://example.services.ai.azure.com/api/projects/test")
    monkeypatch.setenv("FOUNDRY_MODEL", "test-model")
    monkeypatch.delenv("AZURE_FUNCTIONS_AGENTS_ENABLE_COPILOT", raising=False)
    monkeypatch.delenv("ENABLE_SENSITIVE_DATA", raising=False)
    spec = importlib.util.spec_from_file_location("binding_starter_app", src / "function_app.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    functions = module.app.get_functions()
    assert len(functions) == 1  # The endpoint-less agent adds no automatic functions.
    return functions[0]