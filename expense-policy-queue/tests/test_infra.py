import json
import re
import xml.etree.ElementTree as ET
from pathlib import Path

INFRA = Path(__file__).resolve().parent.parent / "infra"


def test_ai_gateway_is_opt_in():
    params = json.loads((INFRA / "main.parameters.json").read_text(encoding="utf-8"))["parameters"]
    assert params["enableAiGateway"]["value"] == "${ENABLE_AI_GATEWAY=false}"
    main = (INFRA / "main.bicep").read_text(encoding="utf-8")
    assert "param enableAiGateway bool = false" in main
    assert re.search(r"module aiGateway '\./app/ai-gateway\.bicep' = if \(enableAiGateway\)", main)
    # Without the gateway the app keeps calling Foundry directly.
    assert "AZURE_FUNCTIONS_AGENTS_PROVIDER: 'foundry'" in main
    assert "AZURE_FUNCTIONS_AGENTS_PROVIDER: 'azure_openai'" in main


def test_ai_gateway_policy_has_every_control():
    inbound = ET.parse(INFRA / "app" / "ai-gateway-policy.xml").getroot().find("inbound")
    tags = [child.tag for child in inbound]
    assert tags == [
        "base",
        "validate-azure-ad-token",
        "llm-token-limit",
        "llm-content-safety",
        "set-backend-service",
        "authentication-managed-identity",
        "llm-emit-token-metric",
    ]
    token = inbound.find("validate-azure-ad-token")
    assert [e.text for e in token.iter("application-id")] == ["__FUNCTION_CLIENT_ID__"]
    assert "https://cognitiveservices.azure.com" in [e.text for e in token.iter("audience")]
    safety = inbound.find("llm-content-safety")
    assert safety.get("shield-prompt") == "true"
    assert {c.get("name") for c in safety.iter("category")} == {"Hate", "SelfHarm", "Sexual", "Violence"}


def test_azure_openai_client_calls_gateway_responses_path():
    from agent_framework.openai import OpenAIChatClient
    from azure.identity.aio import DefaultAzureCredential

    client = OpenAIChatClient(
        model="gpt-5.4", azure_endpoint="https://apim-example.azure-api.net", credential=DefaultAzureCredential()
    )
    # The gateway API is mounted at /openai with a POST /v1/responses operation.
    assert str(client.client.base_url) == "https://apim-example.azure-api.net/openai/v1/"
