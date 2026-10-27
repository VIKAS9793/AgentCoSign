"""The hosted demo's live model budget, so one visitor cannot use up the day.

Edge cases (written before the code):

| Case | Expected |
|---|---|
| Within every limit | The run goes ahead and spends one unit of today's budget |
| Visitor past their own daily allowance | Refused; the shared budget is untouched |
| Visitor running tests too fast | Refused with "wait a few minutes", nothing created |
| Today's shared budget used up | Refused, no money moved, no order created |
| Message too long | Refused before any budget is spent |
| No live models configured | Nothing is spent |
| Groq reports a model's quota | Reaches the guard per model, from primary models only |
| Two models report, one is low | The tightest one decides |
| One model reports none left | Everyone is refused |
| Quota headers missing or not numbers | Ignored |
"""

import json
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import httpx
import pytest
from fastapi.testclient import TestClient
from pydantic import SecretStr

from agentcosign.api.protection import monotonic_ms
from agentcosign.clients.llm import OpenAICompatibleProvider
from agentcosign.core.limits import CapacityMode, LiveDecision, Tier, WorkKind
from agentcosign.main import create_app
from tests.unit.paypal_fixtures import ACCOUNT
from tests.unit.test_gateway_api import Env
from tests.unit.test_llm_client import CONFIG, KEY, MESSAGES, _answer
from tests.unit.test_scenarios import _checks, _runner

LIVE_KEY = "k" * 40
PUBLIC_POOL = (WorkKind.LIVE_LLM, Tier.PUBLIC)
_now = monotonic_ms


def _demo(tmp_path: Path, model_key: str) -> Env:
    env = Env(tmp_path)
    env.settings = env.settings.model_copy(
        update={"env": "demo", "groq_api_key": SecretStr(model_key)}
    )
    env.paypal._merchant_id = ACCOUNT
    env.client = TestClient(create_app(env.settings, env.paypal))
    return env


@pytest.fixture
def demo(tmp_path: Path) -> Iterator[Env]:
    env = _demo(tmp_path, LIVE_KEY)
    with env.client:
        env.enrol()
        _state(env).scenarios = _runner(env, "9.00")
        _checks(env, "clean", "9.00")
        yield env


def _state(env: Env) -> Any:
    return env.client.app.state  # type: ignore[attr-defined]


def _used(env: Env) -> int:
    pool = _state(env).guard._pools[PUBLIC_POOL]
    return int(pool.used)


def _run(env: Env, session: dict[str, str]) -> Any:
    return env.client.post("/v1/demo/scenarios/genuine", headers=session)


def test_a_run_within_every_limit_spends_one_unit(demo: Env) -> None:
    session = demo.session()
    before = _used(demo)
    assert _run(demo, session).status_code == 200
    assert _used(demo) == before + 1


@pytest.mark.edge
def test_a_visitor_past_their_allowance_spends_no_shared_budget(demo: Env) -> None:
    _state(demo).scenarios = _runner(demo, "9.00", per_day=1)
    session = demo.session()
    assert _run(demo, session).status_code == 200
    spent = _used(demo)
    refused = _run(demo, session)
    assert refused.status_code == 429
    assert _used(demo) == spent


@pytest.mark.edge
def test_running_too_fast_is_refused_before_anything_is_created(
    demo: Env, monkeypatch: pytest.MonkeyPatch
) -> None:
    session = demo.session()
    orders = len(demo.paypal._orders)
    monkeypatch.setattr(
        _state(demo).guard,
        "request_live",
        lambda *args: LiveDecision(live=False, reason="client_quota"),
    )
    refused = _run(demo, session)
    assert refused.status_code == 429
    assert refused.json()["detail"].startswith("No money moved.")
    assert "wait a few minutes" in refused.json()["detail"]
    assert len(demo.paypal._orders) == orders


@pytest.mark.edge
def test_a_used_up_day_refuses_runs_and_creates_nothing(demo: Env) -> None:
    session = demo.session()
    pool = _state(demo).guard._pools[PUBLIC_POOL]
    pool.window_start_ms = None
    _state(demo).guard._roll(pool, _now())
    pool.used = pool.limit
    orders = len(demo.paypal._orders)
    refused = _run(demo, session)
    assert refused.status_code == 429
    assert refused.json()["detail"].startswith("No money moved.")
    assert len(demo.paypal._orders) == orders
    steps = demo.client.get("/v1/demo/trace", headers=session).json()["steps"]
    assert steps[-1]["title"] == "Test run not started: live model budget used up"


@pytest.mark.edge
def test_a_too_long_message_spends_no_budget(demo: Env) -> None:
    session = demo.session()
    before = _used(demo)
    too_long = "x" * (demo.settings.demo.message_max_chars + 1)
    response = demo.client.post(
        "/v1/demo/messages", json={"text": too_long}, headers=session
    )
    assert response.status_code == 422
    assert _used(demo) == before


@pytest.mark.edge
def test_without_live_models_nothing_is_spent(tmp_path: Path) -> None:
    env = _demo(tmp_path, "")
    with env.client:
        env.enrol()
        _state(env).scenarios = _runner(env, "9.00")
        _checks(env, "clean", "9.00")
        assert _run(env, env.session()).status_code == 200
        assert _used(env) == 0


# --- Groq's quota headers reach the guard ------------------------------------------

QUOTA_HEADERS = {
    "x-ratelimit-remaining-requests": "40",
    "x-ratelimit-limit-requests": "1000",
}


def _provider(handler: Any) -> tuple[OpenAICompatibleProvider, list[tuple[Any, ...]]]:
    heard: list[tuple[Any, ...]] = []

    def answer(request: httpx.Request) -> httpx.Response:
        return handler(json.loads(request.content)["model"])

    http = httpx.AsyncClient(
        transport=httpx.MockTransport(answer), base_url="https://models.example/v1"
    )
    provider = OpenAICompatibleProvider(
        "screening",
        CONFIG,
        SecretStr(KEY),
        http=http,
        on_quota=lambda *quota: heard.append(quota),
    )
    return provider, heard


@pytest.mark.edge
async def test_only_the_primary_models_quota_is_reported() -> None:
    def handler(model: str) -> httpx.Response:
        if model == "primary-model":
            return httpx.Response(429, headers=QUOTA_HEADERS)
        return httpx.Response(200, json=_answer("ok"), headers=QUOTA_HEADERS)

    provider, heard = _provider(handler)
    await provider.complete(MESSAGES)
    assert heard == [("primary-model", 40, 1000)]


@pytest.mark.edge
async def test_missing_or_garbled_quota_headers_report_nothing_usable() -> None:
    garbled = {"x-ratelimit-remaining-requests": "lots"}

    def handler(model: str) -> httpx.Response:
        return httpx.Response(200, json=_answer("ok"), headers=garbled)

    provider, heard = _provider(handler)
    await provider.complete(MESSAGES)
    assert heard == [("primary-model", None, None)]


@pytest.mark.edge
def test_the_tightest_model_quota_decides(demo: Env) -> None:
    guard = _state(demo).guard
    guard.observe_provider_quota(50, 1000, "openai/gpt-oss-20b")
    assert guard.capacity_mode(_now()) is CapacityMode.PUBLIC_REPLAY
    # a healthy model reporting later does not hide the low one
    guard.observe_provider_quota(900, 1000, "openai/gpt-oss-120b")
    assert guard.capacity_mode(_now()) is CapacityMode.PUBLIC_REPLAY
    guard.observe_provider_quota(0, 1000, "qwen/qwen3.8-27b")
    guard.observe_provider_quota(950, 1000, "meta-llama/llama-prompt-guard-2-86m")
    assert guard.capacity_mode(_now()) is CapacityMode.REPLAY_ONLY
    refused = _run(demo, demo.session())
    assert refused.status_code == 429


@pytest.mark.edge
def test_the_apps_own_models_report_to_the_guard(tmp_path: Path) -> None:
    env = _demo(tmp_path, LIVE_KEY)
    with env.client:
        state = _state(env)
        state.scenarios._assistant._on_quota("openai/gpt-oss-120b", 0, 1000)
        assert state.guard.capacity_mode(_now()) is CapacityMode.REPLAY_ONLY
        state.scenarios._assistant._on_quota("openai/gpt-oss-120b", 900, 1000)
        workspace = env.client.post("/v1/demo/workspaces").json()["workspace"]
        checks = state.workspaces.get(workspace)._checks
        checks._screening._on_quota("openai/gpt-oss-20b", 0, 1000)
        assert state.guard.capacity_mode(_now()) is CapacityMode.REPLAY_ONLY
