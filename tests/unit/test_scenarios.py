"""Ready test scenarios and visitors' own messages on the hosted demo.

Edge cases (written before the code):

| Case | Expected |
|---|---|
| Genuine small refund, checks clean | A real test order, then refunded on its own |
| Fake system message | The assistant proposes the attacker's refund; refused |
| The assistant proposes no refund | The order exists; no decision, nothing moves |
| Daily allowance used up | Refused, nothing created; another workspace unaffected |
| PayPal can't create the order | Error, and the allowance is not used up |
| No assistant (no model key) | Refused before any order is made |
| Own message | Gets its own order id; a card number in it is masked |
| Live order creation, first test card declined | The next card is used |
| Live order creation, every card declined | An error, never a fake order id |
"""

import json
import re
from collections.abc import Iterator, Sequence
from decimal import Decimal
from pathlib import Path
from typing import Any

import httpx
import pytest
from pydantic import SecretStr

from agentcosign.clients.llm import Message, ModelAnswer
from agentcosign.clients.paypal import LivePayPalClient
from agentcosign.core.exceptions import (
    LimitReachedError,
    ModelUnavailableError,
    PayPalClientError,
)
from agentcosign.services.ai_checks import CustomerChecks
from agentcosign.services.scenarios import ScenarioRunner
from tests.unit.paypal_fixtures import ACCOUNT
from tests.unit.test_gateway_api import Env

ORDER = re.compile(r"\b(TEST[0-9A-F]{12})\b")


class _Assistant:
    """Proposes a refund of a fixed amount on the order named in the message."""

    def __init__(self, amount: str | None) -> None:
        self.amount = amount

    async def complete(self, messages: Sequence[Message]) -> ModelAnswer:
        if self.amount is None:
            return ModelAnswer('{"tool": "none"}', "fake")
        order = ORDER.search(messages[-1]["content"])
        body = {
            "tool": "create_refund",
            "order_id": order.group(1) if order else None,
            "amount": self.amount,
            "currency": "USD",
        }
        return ModelAnswer(json.dumps(body), "fake")


class _Model:
    def __init__(self, text: str) -> None:
        self.text = text

    async def complete(self, messages: Sequence[Message]) -> ModelAnswer:
        return ModelAnswer(self.text, "fake")


class _Reader:
    """Reads the order named in the message, as the real reading does."""

    def __init__(self, amount: str) -> None:
        self.amount = amount

    async def complete(self, messages: Sequence[Message]) -> ModelAnswer:
        order = ORDER.search(messages[-1]["content"])
        body = {
            "order_id": order.group(1) if order else None,
            "amount_requested": self.amount,
            "currency": "USD",
            "reason_category": "not_received",
            "wants_other_destination": False,
        }
        return ModelAnswer(json.dumps(body), "fake")


@pytest.fixture
def env(tmp_path: Path) -> Iterator[Env]:
    environment = Env(tmp_path)
    environment.paypal._merchant_id = ACCOUNT  # test orders pay this merchant
    with environment.client:
        yield environment


def _gateway(env: Env) -> Any:
    return env.client.app.state.gateway  # type: ignore[attr-defined]


def _checks(env: Env, verdict: str, amount: str) -> None:
    _gateway(env)._checks = CustomerChecks(
        guard_model=_Model("0.001"),
        screening_model=_Model(json.dumps({"verdict": verdict})),
        reading_model=_Reader(amount),
        threshold=Decimal("0.50"),
    )


def _runner(env: Env, amount: str | None, per_day: int = 15) -> ScenarioRunner:
    return ScenarioRunner(
        paypal=env.paypal, assistant=_Assistant(amount), per_day=per_day
    )


async def test_a_genuine_small_refund_goes_out_on_its_own(env: Env) -> None:
    _checks(env, "clean", "9.00")
    result = await _runner(env, "9.00").run_ready(_gateway(env), "ws-a", "genuine")
    assert result.order_id.startswith("TEST")
    assert result.outcome is not None
    assert result.outcome.decision == "allow"


@pytest.mark.edge
async def test_a_fake_system_message_is_refused(env: Env) -> None:
    _checks(env, "manipulation", "25.00")
    result = await _runner(env, "25.00").run_ready(_gateway(env), "ws-a", "fake_system")
    assert result.outcome is not None
    assert result.outcome.decision == "deny"


@pytest.mark.edge
async def test_no_proposal_means_no_decision(env: Env) -> None:
    _checks(env, "clean", "9.00")
    result = await _runner(env, None).run_ready(_gateway(env), "ws-a", "genuine")
    assert result.outcome is None


@pytest.mark.edge
async def test_the_daily_allowance_is_per_workspace(env: Env) -> None:
    _checks(env, "clean", "9.00")
    runner = _runner(env, "9.00", per_day=1)
    await runner.run_ready(_gateway(env), "ws-a", "genuine")
    with pytest.raises(LimitReachedError):
        await runner.run_ready(_gateway(env), "ws-a", "genuine")
    await runner.run_ready(_gateway(env), "ws-b", "genuine")


@pytest.mark.edge
async def test_a_failed_order_does_not_use_up_the_allowance(env: Env) -> None:
    _checks(env, "clean", "9.00")
    runner = _runner(env, "9.00", per_day=1)

    async def broken(*_: Any) -> str:
        raise PayPalClientError("down", {"retryable": True})

    real = env.paypal.create_test_order
    env.paypal.create_test_order = broken  # type: ignore[method-assign]
    with pytest.raises(PayPalClientError):
        await runner.run_ready(_gateway(env), "ws-a", "genuine")
    env.paypal.create_test_order = real  # type: ignore[method-assign]
    result = await runner.run_ready(_gateway(env), "ws-a", "genuine")
    assert result.outcome is not None


@pytest.mark.edge
async def test_without_an_assistant_no_order_is_made(env: Env) -> None:
    runner = ScenarioRunner(paypal=env.paypal, assistant=None, per_day=15)
    before = len(env.paypal._orders)
    with pytest.raises(ModelUnavailableError):
        await runner.run_ready(_gateway(env), "ws-a", "genuine")
    assert len(env.paypal._orders) == before


@pytest.mark.edge
async def test_an_own_message_gets_its_order_and_is_masked(env: Env) -> None:
    _checks(env, "clean", "15.00")
    runner = _runner(env, "15.00")
    result = await runner.run_own(
        _gateway(env), "ws-a", "Broken mug, refund $15. Card 4111 1111 1111 1111."
    )
    assert result.outcome is not None
    kept = _gateway(env)._messages
    texts = [kept.text(m) for m in _message_ids(env)]
    assert any(result.order_id in t and "[card ending 1111]" in t for t in texts)
    assert not any("4111 1111" in t for t in texts)


def _message_ids(env: Env) -> list[str]:
    import sqlite3

    path = env.settings.sqlite_db_path
    with sqlite3.connect(path) as conn:
        return [row[0] for row in conn.execute("SELECT message_id FROM messages")]


def _live(handler: Any) -> LivePayPalClient:
    def routed(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/v1/oauth2/token":
            return httpx.Response(200, json={"access_token": "t", "expires_in": 3600})
        response: httpx.Response = handler(request)
        return response

    http = httpx.AsyncClient(
        transport=httpx.MockTransport(routed), base_url="https://sandbox.test"
    )
    return LivePayPalClient("id", SecretStr("secret"), http=http)


@pytest.mark.edge
async def test_a_declined_test_card_moves_on_to_the_next() -> None:
    cards: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        card = json.loads(request.content)["payment_source"]["card"]["number"]
        cards.append(card)
        if len(cards) == 1:
            return httpx.Response(422, json={"name": "UNPROCESSABLE_ENTITY"})
        return httpx.Response(201, json={"id": "ORDER123", "status": "COMPLETED"})

    order = await _live(handler).create_test_order(
        Decimal("20.00"), "USD", "Riya Sharma", "demo-1"
    )
    assert order == "ORDER123"
    assert len(cards) == 2 and cards[0] != cards[1]


@pytest.mark.edge
async def test_when_every_card_is_declined_no_order_id_is_made_up() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(422, json={"name": "UNPROCESSABLE_ENTITY"})

    with pytest.raises(PayPalClientError):
        await _live(handler).create_test_order(
            Decimal("20.00"), "USD", "Riya Sharma", "demo-1"
        )


@pytest.mark.edge
async def test_an_order_paypal_did_not_complete_is_never_used() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            201, json={"id": "ORDER9", "status": "PAYER_ACTION_REQUIRED"}
        )

    with pytest.raises(PayPalClientError):
        await _live(handler).create_test_order(
            Decimal("20.00"), "USD", "Riya Sharma", "demo-1"
        )
