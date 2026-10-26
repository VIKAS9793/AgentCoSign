"""The hosted demo's live step record, read through the API.

Edge cases (written before the code):

| Case | Expected |
|---|---|
| Not the hosted demo | The route is not there |
| No sign-in | Refused |
| A scenario runs | The scenario, PayPal, the rules and the record appear in order |
| A step record read | Never carries a secret or the sign-in token |
| Reading again after the last step | Only newer steps; the position stays put |
| A negative position | Refused at the edge |
| Steps in a visitor's workspace | Never in anyone else's record |
| A closed workspace | 410, so the app can set up again |
| A run ends | One closing step: the headline and what moved, from the server |
| A refund PayPal confirmed | Money moved is the amount |
| An attack in the message | Named, and nothing moved |
| An approval with a fingerprint or PIN | Its own closing step after the refund |
| A PayPal call | Carries PayPal's own id and status for what it returned |
"""

import json
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient
from pydantic import SecretStr

from agentcosign.core import trace
from agentcosign.main import create_app
from tests.unit.paypal_fixtures import ACCOUNT
from tests.unit.test_gateway_api import Env
from tests.unit.test_scenarios import _checks, _runner


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


def _read(env: Env, session: dict[str, str], after: int = 0) -> Any:
    response = env.client.get(f"/v1/demo/trace?after={after}", headers=session)
    assert response.status_code == 200
    return response.json()


def _run_genuine(env: Env, session: dict[str, str]) -> None:
    _state(env).scenarios = _runner(env, "9.00")
    _checks(env, "clean", "9.00")
    response = env.client.post("/v1/demo/scenarios/genuine", headers=session)
    assert response.status_code == 200


@pytest.mark.edge
def test_outside_the_demo_the_route_is_not_there(tmp_path: Path) -> None:
    env = Env(tmp_path)
    with env.client:
        env.enrol()
        response = env.client.get("/v1/demo/trace", headers=env.session())
        assert response.status_code == 404


@pytest.mark.edge
def test_reading_the_steps_needs_a_sign_in(demo: Env) -> None:
    assert demo.client.get("/v1/demo/trace").status_code == 401


def test_a_scenario_shows_every_layer_in_order(demo: Env) -> None:
    session = demo.session()
    _run_genuine(demo, session)
    steps = _read(demo, session)["steps"]
    layers = [s["layer"] for s in steps]
    for layer in ("demo", "paypal", "rules", "ledger"):
        assert layer in layers
    assert layers.index("demo") < layers.index("rules") < layers.index("ledger")
    decided = next(s for s in steps if s["layer"] == "rules")
    assert decided["detail"]["decision"] == "allow"
    assert [s["seq"] for s in steps] == sorted({s["seq"] for s in steps})


@pytest.mark.edge
def test_the_steps_never_carry_a_secret_or_the_sign_in(demo: Env) -> None:
    session = demo.session()
    _run_genuine(demo, session)
    text = json.dumps(_read(demo, session))
    secrets = [
        demo.settings.audit_hmac_key.get_secret_value(),
        demo.settings.operator_api_key.get_secret_value(),
        demo.settings.agent_api_key.get_secret_value(),
        session["X-Approver-Session"],
    ]
    for secret in filter(None, secrets):
        assert secret not in text


@pytest.mark.edge
def test_reading_again_gives_only_newer_steps(demo: Env) -> None:
    session = demo.session()
    _run_genuine(demo, session)
    first = _read(demo, session)
    again = _read(demo, session, first["last"])
    assert again == {"steps": [], "last": first["last"]}
    _run_genuine(demo, session)
    newer = _read(demo, session, first["last"])["steps"]
    assert newer
    assert min(s["seq"] for s in newer) == first["last"] + 1


@pytest.mark.edge
def test_a_negative_position_is_refused_at_the_edge(demo: Env) -> None:
    response = demo.client.get("/v1/demo/trace?after=-1", headers=demo.session())
    assert response.status_code == 422


@pytest.mark.edge
def test_a_workspace_step_never_reaches_another_record(demo: Env) -> None:
    session = demo.session()
    workspace = demo.client.post("/v1/demo/workspaces").json()["workspace"]
    record = _state(demo).workspaces.trace(workspace)
    token = trace.use(record)
    try:
        trace.emit("demo", "Only in the visitor's workspace")
    finally:
        trace._current.reset(token)
    titles = [s.title for s in record.after(0)]
    assert "Only in the visitor's workspace" in titles
    assert "Private workspace opened" in titles
    seen = [s["title"] for s in _read(demo, session)["steps"]]
    assert "Only in the visitor's workspace" not in seen
    assert "Private workspace opened" not in seen


@pytest.mark.edge
def test_a_closed_workspace_answers_gone(demo: Env) -> None:
    response = demo.client.get(
        "/v1/demo/trace", headers={"x-workspace": "ws-" + "0" * 32}
    )
    assert response.status_code == 410


def _outcome(env: Env, session: dict[str, str]) -> Any:
    steps = _read(env, session)["steps"]
    assert steps[-1]["layer"] == "outcome"
    return steps[-1]


def test_a_confirmed_refund_ends_with_what_moved(demo: Env) -> None:
    session = demo.session()
    _run_genuine(demo, session)
    end = _outcome(demo, session)
    assert end["title"] == "Refund of $9.00 sent automatically: PayPal confirmed"
    assert end["detail"]["money_moved"] == "$9.00"
    assert int(end["detail"]["paypal_calls"]) >= 1
    assert int(end["detail"]["record_entries"]) >= 1


@pytest.mark.edge
def test_an_attack_ends_with_nothing_moved(demo: Env) -> None:
    session = demo.session()
    _state(demo).scenarios = _runner(demo, "9.00")
    _checks(demo, "manipulation", "9.00")
    demo.client.post("/v1/demo/scenarios/fake_system", headers=session)
    steps = _read(demo, session)["steps"]
    screening = next(s for s in steps if s["title"].startswith("Safety screening"))
    assert screening["detail"]["safe"].startswith("Attack found")
    end = _outcome(demo, session)
    assert end["title"].startswith("Attack in the customer's message caught")
    assert end["detail"]["money_moved"] == "$0.00"


@pytest.mark.edge
def test_an_approval_ends_with_its_own_outcome(demo: Env) -> None:
    demo.propose("r1", "10.00")
    session = demo.session()
    response, _ = demo.decide("r1", "approve", session)
    assert response.json()["paypal_status"] == "COMPLETED"
    end = _outcome(demo, session)
    assert end["title"] == (
        "Refund of $10.00 sent after the merchant's fingerprint or PIN: "
        "PayPal confirmed"
    )


@pytest.mark.edge
def test_a_declined_approval_ends_with_nothing_sent(demo: Env) -> None:
    demo.propose("r1", "10.00")
    session = demo.session()
    demo.decide("r1", "reject", session)
    end = _outcome(demo, session)
    assert end["title"] == "Declined by the merchant: nothing sent"
    assert end["detail"]["money_moved"] == "$0.00"


@pytest.mark.edge
def test_health_says_whether_the_models_are_live(tmp_path: Path) -> None:
    for key, expected in (("", "off"), ("k" * 40, "live")):
        env = Env(tmp_path / expected)
        settings = env.settings.model_copy(update={"groq_api_key": SecretStr(key)})
        with TestClient(create_app(settings, env.paypal)) as client:
            assert client.get("/health").json()["ai_models"] == expected
