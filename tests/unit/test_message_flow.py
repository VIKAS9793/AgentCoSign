"""A customer's message through the gateway: kept masked, checked by the AI,
and decided by the rules with the checks' results.

Edge cases (written before the code):

| Case | Expected |
|---|---|
| Words match, screening clean, small amount | Sent without the merchant |
| Customer asked for a different amount | Merchant decides |
| Screening finds manipulation | Refused |
| Message id unknown to the gateway | Unchecked: merchant decides |
| Checks switched off (no model key) | Unchecked: merchant decides |
| The same request sent twice | Models asked once; same outcome |
| Later re-checks of the request | Use the saved results, not a new run |
| A malformed message id | Refused at the edge |
"""

from collections.abc import Iterator, Sequence
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest

from agentcosign.clients.llm import Message, ModelAnswer
from agentcosign.models.policy import ReadingStatus, ScreeningVerdict
from agentcosign.services.ai_checks import CustomerChecks
from tests.scenarios.policy_scenarios import NOW
from tests.unit.test_gateway_api import Env


class _Model:
    def __init__(self, text: str) -> None:
        self.text = text
        self.calls = 0

    async def complete(self, messages: Sequence[Message]) -> ModelAnswer:
        self.calls += 1
        return ModelAnswer(self.text, "fake-model")


def _reading(amount: str) -> str:
    return (
        f'{{"order_id": "ORD-1", "amount_requested": "{amount}", "currency": "USD", '
        '"reason_category": "not_received", "wants_other_destination": false}'
    )


@pytest.fixture
def env(tmp_path: Path) -> Iterator[Env]:
    environment = Env(tmp_path)
    with environment.client:
        yield environment


def _gateway(env: Env) -> Any:
    return env.client.app.state.gateway  # type: ignore[attr-defined]


def _install(env: Env, verdict: str = "clean", asked: str = "10.00") -> _Model:
    screening = _Model(f'{{"verdict": "{verdict}"}}')
    _gateway(env)._checks = CustomerChecks(
        guard_model=_Model("0.001"),
        screening_model=screening,
        reading_model=_Model(_reading(asked)),
        threshold=Decimal("0.50"),
    )
    return screening


def _message(env: Env, text: str = "Order ORD-1 never came, refund $10") -> str:
    return str(_gateway(env)._messages.add(text, NOW))


def test_matching_words_let_a_small_refund_go_alone(env: Env) -> None:
    _install(env)
    body = env.propose("m1", "10.00", message_id=_message(env)).json()
    assert body["decision"] == "allow"
    assert body["rule_id"] == "R1"


@pytest.mark.edge
def test_a_different_amount_in_the_customers_words_asks_the_merchant(
    env: Env,
) -> None:
    _install(env, asked="25.00")
    body = env.propose("m1", "10.00", message_id=_message(env)).json()
    assert body["decision"] == "escalate"
    assert body["rule_id"] == "R18"


@pytest.mark.edge
def test_manipulation_in_the_message_is_refused(env: Env) -> None:
    _install(env, verdict="manipulation")
    body = env.propose("m1", "10.00", message_id=_message(env)).json()
    assert body["decision"] == "deny"
    assert body["rule_id"] == "R11"


@pytest.mark.edge
def test_an_unknown_message_is_unchecked(env: Env) -> None:
    _install(env)
    body = env.propose("m1", "10.00", message_id="msg-0000000000000000").json()
    assert body["decision"] == "escalate"
    assert body["rule_id"] == "R15"


@pytest.mark.edge
def test_without_the_checks_a_message_is_unchecked(env: Env) -> None:
    _gateway(env)._checks = None
    body = env.propose("m1", "10.00", message_id=_message(env)).json()
    assert body["decision"] == "escalate"
    assert body["rule_id"] == "R15"


@pytest.mark.edge
def test_a_repeated_request_asks_the_models_once(env: Env) -> None:
    screening = _install(env, asked="25.00")
    message_id = _message(env)
    first = env.propose("m1", "10.00", message_id=message_id).json()
    again = env.propose("m1", "10.00", message_id=message_id).json()
    assert screening.calls == 1
    assert again["decision"] == first["decision"]


@pytest.mark.edge
async def test_later_rechecks_use_the_saved_results(env: Env) -> None:
    screening = _install(env, asked="25.00")
    env.propose("m1", "10.00", message_id=_message(env))
    signals = await _gateway(env)._signals("m1")
    assert signals.screening is ScreeningVerdict.CLEAN
    assert signals.reading_status is ReadingStatus.READ
    assert screening.calls == 1


@pytest.mark.edge
def test_a_malformed_message_id_is_refused_at_the_edge(env: Env) -> None:
    _install(env)
    response = env.propose("m1", "10.00", message_id="../etc/passwd")
    assert response.status_code == 422


@pytest.mark.edge
async def test_the_card_says_what_the_customer_asked_for(env: Env) -> None:
    _install(env, asked="25.00")
    env.propose("m1", "10.00", message_id=_message(env))
    line = await _gateway(env)._customer_asked("m1", "ORD-1")
    assert line.startswith("Customer asked for $25.00. Reason")
    assert "item didn't arrive" in line
    assert await _gateway(env)._customer_asked("no-such-request") == ""
