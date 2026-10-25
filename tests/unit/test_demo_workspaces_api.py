"""Opening a private workspace on the hosted demo, through the API.

Edge cases (written before the code):

| Case | Expected |
|---|---|
| Not the hosted demo | No workspaces: the route is not there |
| Hosted demo | A workspace id and a one-time setup token for it |
| A setup token from one workspace | Refused by every other one and the owner's |
| A closed or unknown workspace on a request | 410, so the app can ask to set up again |
| Two workspaces | Separate databases |
| Too many setups from one address | 429, nothing created |
"""

from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest

from tests.unit.test_gateway_api import Env


@pytest.fixture
def demo(tmp_path: Path) -> Iterator[Env]:
    environment = Env(tmp_path)
    environment.settings = environment.settings.model_copy(update={"env": "demo"})
    from fastapi.testclient import TestClient

    from agentcosign.main import create_app

    environment.client = TestClient(
        create_app(environment.settings, environment.paypal)
    )
    with environment.client:
        yield environment


def _open(env: Env) -> Any:
    return env.client.post("/v1/demo/workspaces")


def _workspaces(env: Env) -> Any:
    return env.client.app.state.workspaces  # type: ignore[attr-defined]


@pytest.mark.edge
def test_outside_the_demo_no_workspace_can_be_opened(tmp_path: Path) -> None:
    env = Env(tmp_path)
    with env.client:
        assert _open(env).status_code == 404


def test_the_demo_opens_a_workspace_with_its_own_setup_token(demo: Env) -> None:
    body = _open(demo).json()
    gateway = _workspaces(demo).get(body["workspace"])
    assert gateway is not None
    assert gateway.enrolment_token_is_valid(body["enrolment"])


@pytest.mark.edge
def test_a_setup_token_works_only_in_its_own_workspace(demo: Env) -> None:
    first, second = _open(demo).json(), _open(demo).json()
    registry = _workspaces(demo)
    assert not registry.get(second["workspace"]).enrolment_token_is_valid(
        first["enrolment"]
    )
    assert not registry.default.enrolment_token_is_valid(first["enrolment"])


@pytest.mark.edge
def test_a_closed_workspace_answers_gone(demo: Env) -> None:
    response = demo.client.get(
        "/v1/approvals/home", headers={"x-workspace": "ws-" + "0" * 32}
    )
    assert response.status_code == 410


@pytest.mark.edge
def test_two_workspaces_never_share_a_database(demo: Env) -> None:
    first, second = _open(demo).json(), _open(demo).json()
    registry = _workspaces(demo)
    one = registry.get(first["workspace"])._ledger._path
    two = registry.get(second["workspace"])._ledger._path
    assert one != two


@pytest.mark.edge
def test_too_many_setups_from_one_address_are_refused(demo: Env) -> None:
    allowed = demo.settings.demo.setups_per_address_per_day
    for _ in range(allowed):
        assert _open(demo).status_code == 200
    assert _open(demo).status_code == 429
