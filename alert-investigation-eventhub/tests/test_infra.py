# Copyright (c) Microsoft Corporation.
# Licensed under the MIT License.

import json
import re
from pathlib import Path

TEMPLATE = Path(__file__).resolve().parent.parent


def test_decision_model_deployment_matches_local_and_app_settings():
    main = (TEMPLATE / "infra" / "main.bicep").read_text(encoding="utf-8")
    foundry = (TEMPLATE / "infra" / "app" / "foundry.bicep").read_text(encoding="utf-8")
    local = json.loads((TEMPLATE / "src" / "local.settings.json.sample").read_text(encoding="utf-8"))

    for template in (main, foundry):
        assert "param decisionDeploymentName string = 'decision-1.1'" in template
    assert "param decisionModelVersion string = '1'" in foundry
    assert re.search(
        r"resource decisionModelDeployment\s+'.*?'\s+=\s+\{.*?"
        r"model:\s+\{\s+format:\s+'Microsoft'\s+name:\s+'Microsoft-Decision-1\.1'"
        r"\s+version:\s+decisionModelVersion\s+\}",
        foundry,
        re.DOTALL,
    )
    assert "decisionDeploymentName: decisionDeploymentName" in main
    assert "DECISION_DEPLOYMENT: foundry.outputs.decisionDeploymentName" in main
    assert "output DECISION_DEPLOYMENT string = foundry.outputs.decisionDeploymentName" in main
    assert "output decisionDeploymentName string = decisionModelDeployment.name" in foundry
    assert local["Values"]["DECISION_DEPLOYMENT"] == "decision-1.1"
