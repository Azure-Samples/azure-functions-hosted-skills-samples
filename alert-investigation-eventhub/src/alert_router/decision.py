# Copyright (c) Microsoft Corporation.
# Licensed under the MIT License.

"""Minimal client for the Microsoft-Decision-1 decision API in Microsoft Foundry.

``POST {resource endpoint}/providers/microsoft/v1/systemone`` with a ``state`` and typed
``questions``. The model returns a typed answer per question — for a ``choice`` question,
the selected option and a probability for every option — instead of generated text.
See https://learn.microsoft.com/azure/foundry/foundry-models/how-to/use-foundry-models-microsoft-decision
"""

from __future__ import annotations

import json
import os
import time
import urllib.error
import urllib.request
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Any

TOKEN_SCOPE = "https://cognitiveservices.azure.com/.default"
API_PATH = "/providers/microsoft/v1/systemone"
_RETRYABLE = {408, 429, 500, 502, 503, 504}

# post(url, headers, body) -> (status, response body, response headers). Swappable in tests.
PostFn = Callable[[str, dict[str, str], bytes], tuple[int, bytes, Mapping[str, str]]]
MAX_RETRY_WAIT_SECONDS = 30.0


class DecisionError(RuntimeError):
    """The decision API could not return a valid answer. Callers must fail safe."""


@dataclass(frozen=True)
class ChoiceAnswer:
    choice: str
    probabilities: dict[str, float]

    @property
    def confidence(self) -> float:
        return self.probabilities.get(self.choice, 0.0)


def _urllib_post(
    url: str, headers: dict[str, str], body: bytes, timeout: float = 30
) -> tuple[int, bytes, Mapping[str, str]]:
    request = urllib.request.Request(url, data=body, headers=headers, method="POST")
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return response.status, response.read(), {k.lower(): v for k, v in response.headers.items()}
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read(), {k.lower(): v for k, v in exc.headers.items()}
    except (urllib.error.URLError, TimeoutError) as exc:
        raise DecisionError(f"could not reach the decision API: {exc}") from exc


def retry_delay(headers: Mapping[str, str], fallback: float) -> float:
    """Seconds to wait before retrying, from ``retry-after-ms`` or ``retry-after`` when present."""
    for key, scale in (("retry-after-ms", 0.001), ("retry-after", 1.0)):
        try:
            return min(MAX_RETRY_WAIT_SECONDS, max(0.0, float(headers[key]) * scale))
        except (KeyError, TypeError, ValueError):
            continue
    return min(MAX_RETRY_WAIT_SECONDS, fallback)


def parse_choice(payload: Any, question: str, options: Mapping[str, str]) -> ChoiceAnswer:
    """Read ``answers[question]`` from a decision API response."""
    answers = payload.get("answers") if isinstance(payload, dict) else None
    answer = answers.get(question) if isinstance(answers, dict) else None
    if not isinstance(answer, dict):
        raise DecisionError(f"response has no answer for {question!r}")
    choice = answer.get("choice")
    if choice not in options:
        raise DecisionError(f"unsupported choice {choice!r}")

    probabilities: dict[str, float] = {}
    for key in ("probabilities", "probability", "scores"):
        value = answer.get(key)
        if isinstance(value, dict):
            probabilities = {str(k): float(v) for k, v in value.items() if isinstance(v, int | float)}
            break
    return ChoiceAnswer(choice=choice, probabilities=probabilities)


class DecisionClient:
    def __init__(
        self,
        endpoint: str,
        deployment: str,
        *,
        token_provider: Callable[[], str],
        post: PostFn | None = None,
        max_attempts: int = 6,
        backoff_seconds: float = 2.0,
    ) -> None:
        if not endpoint or not deployment:
            raise DecisionError("DECISION_ENDPOINT and DECISION_DEPLOYMENT must be set")
        self.url = endpoint.rstrip("/") + API_PATH
        self.deployment = deployment
        self._token_provider = token_provider
        self._post = post or _urllib_post
        self._max_attempts = max_attempts
        self._backoff = backoff_seconds

    @classmethod
    def from_environment(cls) -> DecisionClient:
        from azure.identity import DefaultAzureCredential, get_bearer_token_provider

        credential = DefaultAzureCredential(managed_identity_client_id=os.environ.get("AZURE_CLIENT_ID"))
        return cls(
            os.environ.get("DECISION_ENDPOINT", ""),
            os.environ.get("DECISION_DEPLOYMENT", ""),
            token_provider=get_bearer_token_provider(credential, TOKEN_SCOPE),
        )

    def decide(self, state: Any, questions: dict[str, Any]) -> dict[str, Any]:
        """Send one request and return the parsed JSON response."""
        body = json.dumps({"model": self.deployment, "state": state, "questions": questions}).encode("utf-8")
        error = ""
        for attempt in range(1, self._max_attempts + 1):
            headers = {
                "Authorization": f"Bearer {self._token_provider()}",
                "Content-Type": "application/json",
                "Accept": "application/json",
            }
            status, data, response_headers = self._post(self.url, headers, body)
            if 200 <= status < 300:
                try:
                    return json.loads(data)
                except (json.JSONDecodeError, UnicodeDecodeError) as exc:
                    raise DecisionError("decision API returned invalid JSON") from exc
            error = f"HTTP {status}: {data[:300].decode('utf-8', errors='replace')}"
            if status not in _RETRYABLE or attempt == self._max_attempts:
                break
            time.sleep(retry_delay(response_headers or {}, self._backoff * 2 ** (attempt - 1)))
        raise DecisionError(f"decision API failed: {error}")

    def choose(self, state: Any, question: str, instructions: str, options: Mapping[str, str]) -> ChoiceAnswer:
        payload = self.decide(
            state,
            {question: {"type": "choice", "instructions": instructions, "criteria": dict(options)}},
        )
        return parse_choice(payload, question, options)
