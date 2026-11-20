# Copyright (c) Microsoft Corporation.
# Licensed under the MIT License.

import json
from unittest.mock import Mock

import pytest
from alert_router.decision import ChoiceAnswer, DecisionClient, DecisionError, parse_choice

OPTIONS = {"investigate": "a", "aggregate": "b", "routine": "c", "uncertain": "d"}


class FakeHttp:
    def __init__(self, *responses):
        self.responses = list(responses)
        self.calls = []

    def __call__(self, url, headers, body):
        self.calls.append((url, headers, json.loads(body)))
        status, payload, *rest = self.responses.pop(0)
        return status, payload if isinstance(payload, bytes) else json.dumps(payload).encode(), rest[0] if rest else {}


def client(http, *, deployment="decision-1.1", **kwargs):
    return DecisionClient(
        "https://cog-x.cognitiveservices.azure.com/", deployment,
        token_provider=lambda: "token", post=http, backoff_seconds=0, **kwargs,
    )


ANSWER = {"answers": {"route": {"type": "choice", "choice": "routine",
                                "probabilities": {"investigate": 0.05, "aggregate": 0.1, "routine": 0.8, "uncertain": 0.05}}}}


@pytest.mark.parametrize("deployment", ["decision-1.1", "my-routing-deployment"])
def test_choose_sends_request_and_parses_answer(deployment):
    http = FakeHttp((200, ANSWER))
    answer = client(http, deployment=deployment).choose({"alert": 1}, "route", "Pick one", OPTIONS)
    assert answer == ChoiceAnswer("routine", ANSWER["answers"]["route"]["probabilities"])
    assert answer.confidence == 0.8
    url, headers, body = http.calls[0]
    assert url == "https://cog-x.cognitiveservices.azure.com/providers/microsoft/v1/systemone"
    assert headers["Authorization"] == "Bearer token"
    assert body["model"] == deployment
    assert body["state"] == {"alert": 1}
    assert body["questions"]["route"] == {"type": "choice", "instructions": "Pick one", "criteria": OPTIONS}


def test_from_environment_uses_the_configured_deployment(monkeypatch):
    credential = object()
    credential_factory = Mock(return_value=credential)
    token_factory = Mock(return_value=lambda: "token")
    http = FakeHttp((200, ANSWER))
    monkeypatch.setenv("DECISION_ENDPOINT", "https://cog-x.cognitiveservices.azure.com/")
    monkeypatch.setenv("DECISION_DEPLOYMENT", "decision-1.1")
    monkeypatch.setenv("AZURE_CLIENT_ID", "identity-client-id")
    monkeypatch.setattr("azure.identity.DefaultAzureCredential", credential_factory)
    monkeypatch.setattr("azure.identity.get_bearer_token_provider", token_factory)
    monkeypatch.setattr("alert_router.decision._urllib_post", http)

    answer = DecisionClient.from_environment().choose({}, "route", "Pick one", OPTIONS)

    assert answer.choice == "routine"
    assert http.calls[0][2]["model"] == "decision-1.1"
    credential_factory.assert_called_once_with(managed_identity_client_id="identity-client-id")
    token_factory.assert_called_once_with(credential, "https://cognitiveservices.azure.com/.default")


def test_retries_throttling_then_succeeds(monkeypatch):
    waits = []
    monkeypatch.setattr("alert_router.decision.time.sleep", waits.append)
    http = FakeHttp((429, b"slow down", {"retry-after": "7"}), (200, ANSWER))
    assert client(http).choose({}, "route", "x", OPTIONS).choice == "routine"
    assert len(http.calls) == 2
    assert waits == [7.0]


def test_retry_delay():
    from alert_router.decision import retry_delay

    assert retry_delay({"retry-after-ms": "1500"}, 9) == 1.5
    assert retry_delay({"retry-after": "120"}, 9) == 30.0
    assert retry_delay({"retry-after": "Wed, 21 Oct 2026 07:28:00 GMT"}, 4) == 4
    assert retry_delay({}, 2) == 2


@pytest.mark.parametrize("status", [401, 403, 404, 422])
def test_client_errors_are_not_retried(status):
    http = FakeHttp((status, b'{"error": "nope"}'))
    with pytest.raises(DecisionError, match=str(status)):
        client(http).choose({}, "route", "x", OPTIONS)
    assert len(http.calls) == 1


def test_gives_up_after_max_attempts():
    http = FakeHttp((503, b""), (503, b""), (503, b""))
    with pytest.raises(DecisionError):
        client(http, max_attempts=3).choose({}, "route", "x", OPTIONS)
    assert len(http.calls) == 3


def test_invalid_json_raises():
    with pytest.raises(DecisionError):
        client(FakeHttp((200, b"<html>"))).choose({}, "route", "x", OPTIONS)


@pytest.mark.parametrize("payload", [{}, {"answers": {}}, {"answers": {"route": {"choice": "delete_everything"}}}])
def test_parse_choice_rejects_missing_or_unknown(payload):
    with pytest.raises(DecisionError):
        parse_choice(payload, "route", OPTIONS)


def test_parse_choice_without_probabilities():
    answer = parse_choice({"answers": {"route": {"choice": "aggregate"}}}, "route", OPTIONS)
    assert answer.choice == "aggregate" and answer.probabilities == {} and answer.confidence == 0.0


def test_requires_endpoint_and_deployment():
    with pytest.raises(DecisionError):
        DecisionClient("", "x", token_provider=lambda: "t")
