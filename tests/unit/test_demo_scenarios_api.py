"""Running test scenarios through the hosted demo's API.

Edge cases (written before the code):

| Case | Expected |
|---|---|
| Not the hosted demo | The routes are not there |
| No sign-in | Refused |
| Ready scenario | A test order and the decision, as the merchant would see it |
| Unknown scenario | Refused at the edge |
| Own message over the configured length | Refused, nothing created |
| Allowance used up | 429 with the reason |
| No assistant | 503, no money moved |
| PayPal can't create the order | 502, no money moved |
"""

import json
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from agentcosign.core.exceptions import PayPalClientError
from agentcosign.main import create_app
from agentcosign.services.scenarios import ScenarioRunner
from tests.unit.paypal_fixtures import ACCOUNT
from tests.unit.test_gateway_api import Env
from tests.unit.test_scenarios import _Assistant, _checks


@pytest.fixture
def demo(tmp_path: Path) -> Iterator[Env]:
    env = Env(tmp_path)
    env.settings = env.settings.model_copy(update={"env": "demo"})
    env.paypal._merchant_id = ACCOUNT
    env.client = TestClient(create_app(env.settings, env.paypal))
    with env.client:
        env.enrol()
        yield env


def _state(env: Env) -> Any:
    return env.client.app.state  # type: ignore[attr-defined]


def _runner(env: Env, amount: str | None = "9.00", per_day: int = 15) -> None:
    _state(env).scenarios = ScenarioRunner(
        paypal=env.paypal, assistant=_Assistant(amount), per_day=per_day
    )


def _post(env: Env, path: str, body: dict[str, str] | None = None) -> Any:
    return env.client.post(path, json=body or {}, headers=env.session())


def test_outside_the_demo_the_routes_are_not_there(tmp_path: Path) -> None:
    env = Env(tmp_path)
    with env.client:
        assert env.client.get("/v1/demo/scenarios").status_code == 404


def test_the_ready_scenarios_are_listed_with_the_allowance(demo: Env) -> None:
    body = demo.client.get("/v1/demo/scenarios").json()
    assert [s["key"] for s in body["scenarios"]][0] == "genuine"
    assert body["per_day"] == demo.settings.demo.scenarios_per_workspace_per_day


@pytest.mark.edge
def test_running_a_scenario_needs_a_sign_in(demo: Env) -> None:
    assert demo.client.post("/v1/demo/scenarios/genuine").status_code == 401


def test_a_ready_scenario_returns_the_order_and_the_decision(demo: Env) -> None:
    _runner(demo)
    _checks(demo, "clean", "9.00")
    body = _post(demo, "/v1/demo/scenarios/genuine").json()
    assert body["order_id"].startswith("TEST")
    assert body["outcome"]["decision"] == "allow"


@pytest.mark.edge
def test_an_unknown_scenario_is_refused_at_the_edge(demo: Env) -> None:
    assert _post(demo, "/v1/demo/scenarios/steal").status_code == 422


@pytest.mark.edge
def test_an_own_message_over_the_limit_is_refused(demo: Env) -> None:
    _runner(demo)
    before = len(demo.paypal._orders)
    too_long = "x" * (demo.settings.demo.message_max_chars + 1)
    response = _post(demo, "/v1/demo/messages", {"text": too_long})
    assert response.status_code == 422
    assert len(demo.paypal._orders) == before


@pytest.mark.edge
def test_a_used_up_allowance_says_why(demo: Env) -> None:
    _runner(demo, per_day=1)
    _checks(demo, "clean", "9.00")
    _post(demo, "/v1/demo/scenarios/genuine")
    response = _post(demo, "/v1/demo/scenarios/genuine")
    assert response.status_code == 429


@pytest.mark.edge
def test_without_an_assistant_nothing_moves(demo: Env) -> None:
    _state(demo).scenarios = ScenarioRunner(
        paypal=demo.paypal, assistant=None, per_day=15
    )
    response = _post(demo, "/v1/demo/scenarios/genuine")
    assert response.status_code == 503
    assert "No money moved" in response.json()["detail"]


@pytest.mark.edge
def test_a_paypal_failure_says_no_money_moved(demo: Env) -> None:
    _runner(demo)

    async def broken(*_: Any) -> str:
        raise PayPalClientError("down", {"retryable": True})

    demo.paypal.create_test_order = broken  # type: ignore[method-assign]
    response = _post(demo, "/v1/demo/messages", {"text": json.dumps("hi")})
    assert response.status_code == 502
    assert "No money moved" in response.json()["detail"]
