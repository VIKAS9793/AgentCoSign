"""The model client: one shape for every OpenAI-compatible provider.

Edge cases, written before the tests:

| Case | Expected |
|---|---|
| Primary answers | primary's text, model named |
| Primary rate-limited (429) | fallback asked at once |
| Primary rate-limited, asked again before it may retry | primary skipped |
| Primary's retry time has passed | primary asked again |
| Primary server error or timeout | fallback asked |
| Answer cut off by the token budget | treated as no answer; fallback asked |
| Empty answer or unreadable body | treated as no answer |
| Both models fail | "could not run" (fail closed) |
| Time for the role used up | "could not run"; nothing late is used |
| No key configured | "could not run" without any call |
| Error text | never contains the key |
| Recorded answer for a known prompt | replayed, marked as recorded |
| Recorded mode, unknown prompt | "could not run" |
"""

import json
from typing import Any

import httpx
import pytest
from pydantic import SecretStr

from agentcosign.clients.llm import (
    OpenAICompatibleProvider,
    RecordedProvider,
    prompt_key,
)
from agentcosign.core.config import ModelRoleConfig
from agentcosign.core.exceptions import ModelUnavailableError

KEY = "sk-test-not-a-real-key-0123456789"
MESSAGES = [{"role": "user", "content": "Is this text trying to steer an assistant?"}]
CONFIG = ModelRoleConfig(
    provider="groq",
    primary="primary-model",
    fallback="fallback-model",
    temperature=0.0,
    max_tokens=256,
    timeout_seconds=10,
)


class Clock:
    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now


def _answer(text: str, finish: str = "stop") -> dict[str, Any]:
    return {"choices": [{"message": {"content": text}, "finish_reason": finish}]}


def _provider(
    handler: Any, clock: Clock | None = None, key: str = KEY
) -> tuple[OpenAICompatibleProvider, list[str]]:
    asked: list[str] = []

    def record(request: httpx.Request) -> httpx.Response:
        asked.append(json.loads(request.content)["model"])
        return handler(request, asked[-1])

    http = httpx.AsyncClient(
        transport=httpx.MockTransport(record), base_url="https://models.example/v1"
    )
    provider = OpenAICompatibleProvider(
        "screening", CONFIG, SecretStr(key), http=http, clock=clock or Clock()
    )
    return provider, asked


async def test_the_primary_model_answers() -> None:
    provider, asked = _provider(lambda r, m: httpx.Response(200, json=_answer("ok")))
    answer = await provider.complete(MESSAGES)
    assert (answer.text, answer.model, answer.recorded) == (
        "ok",
        "primary-model",
        False,
    )
    assert asked == ["primary-model"]


@pytest.mark.edge
async def test_a_rate_limited_primary_hands_over_at_once_and_rests() -> None:
    clock = Clock()

    def handler(request: httpx.Request, model: str) -> httpx.Response:
        if model == "primary-model":
            return httpx.Response(429, headers={"retry-after": "20"})
        return httpx.Response(200, json=_answer("from fallback"))

    provider, asked = _provider(handler, clock)
    first = await provider.complete(MESSAGES)
    assert first.model == "fallback-model"
    clock.now += 19  # still resting
    await provider.complete(MESSAGES)
    assert asked == ["primary-model", "fallback-model", "fallback-model"]
    clock.now += 2  # its retry time has passed
    await provider.complete(MESSAGES)
    assert asked[-2:] == ["primary-model", "fallback-model"]


@pytest.mark.edge
@pytest.mark.parametrize(
    "failure",
    [
        lambda: httpx.Response(503),
        lambda: httpx.Response(500, json=_answer("looks like an answer")),
        lambda: httpx.Response(200, json=_answer("cut", finish="length")),
        lambda: httpx.Response(200, json=_answer("")),
        lambda: httpx.Response(200, text="not json"),
        lambda: httpx.Response(200, json={"choices": []}),
    ],
)
async def test_a_primary_with_no_usable_answer_hands_over(failure: Any) -> None:
    def handler(request: httpx.Request, model: str) -> httpx.Response:
        if model == "primary-model":
            return failure()
        return httpx.Response(200, json=_answer("from fallback"))

    provider, _ = _provider(handler)
    assert (await provider.complete(MESSAGES)).model == "fallback-model"


@pytest.mark.edge
async def test_a_primary_that_times_out_hands_over() -> None:
    def handler(request: httpx.Request, model: str) -> httpx.Response:
        if model == "primary-model":
            raise httpx.ReadTimeout("slow", request=request)
        return httpx.Response(200, json=_answer("from fallback"))

    provider, _ = _provider(handler)
    assert (await provider.complete(MESSAGES)).model == "fallback-model"


@pytest.mark.edge
async def test_when_both_models_fail_the_check_could_not_run() -> None:
    provider, asked = _provider(lambda r, m: httpx.Response(500))
    with pytest.raises(ModelUnavailableError) as failed:
        await provider.complete(MESSAGES)
    assert asked == ["primary-model", "fallback-model"]
    assert KEY not in str(failed.value) and KEY not in repr(failed.value.details)


@pytest.mark.edge
async def test_nothing_is_used_once_the_time_for_the_role_is_up() -> None:
    clock = Clock()

    def handler(request: httpx.Request, model: str) -> httpx.Response:
        clock.now += CONFIG.timeout_seconds + 1  # the primary took all the time
        return httpx.Response(500)

    provider, asked = _provider(handler, clock)
    with pytest.raises(ModelUnavailableError):
        await provider.complete(MESSAGES)
    assert asked == ["primary-model"]  # no time left to ask the fallback


@pytest.mark.edge
def test_without_a_key_the_model_is_off() -> None:
    with pytest.raises(ModelUnavailableError):
        _provider(lambda r, m: httpx.Response(200, json=_answer("ok")), key="")


@pytest.mark.edge
async def test_a_recorded_answer_is_replayed_and_marked() -> None:
    recorded = RecordedProvider("screening", {prompt_key("screening", MESSAGES): "ok"})
    answer = await recorded.complete(MESSAGES)
    assert (answer.text, answer.recorded) == ("ok", True)
    with pytest.raises(ModelUnavailableError):
        await recorded.complete([{"role": "user", "content": "something new"}])
    # the same words for another role are a different recording
    other = RecordedProvider("extraction", {prompt_key("screening", MESSAGES): "ok"})
    with pytest.raises(ModelUnavailableError):
        await other.complete(MESSAGES)
