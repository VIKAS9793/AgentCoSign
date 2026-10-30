"""Fake webhooks must not be able to spend PayPal's rate limit.

Anyone can POST to the webhook address, and every event that passes the cheap
checks makes the service call PayPal to verify it. Rate limits per client do not
help when the sender rotates addresses, so the calls have a shared budget.

Edge cases (written before the code):

| Case | Expected |
|---|---|
| Certificate address on PayPal's sandbox or live host | Sent to PayPal to verify |
| Certificate over plain http | Refused, PayPal never called |
| Certificate on another host, or a look-alike | Refused, PayPal never called |
| A user name before the host (paypal.com@evil.example) | Refused, PayPal never called |
| Certificate address is not a valid URL | Refused, PayPal never called |
| More checks in a minute than the budget | 429 so PayPal retries, PayPal not called |
| A refused fake | Spends no budget |
| The next minute | The budget is back |
"""

from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from agentcosign.core.limits import TrafficGuard
from agentcosign.main import create_app
from tests.unit.test_gateway_api import SIGNED, Env, _event

CERT = "PAYPAL-CERT-URL"


def _app(env: Env, budget: int = 30) -> list[str]:
    """The service in live-sandbox mode; returns the list of PayPal verify calls."""
    limits = env.settings.limits.model_copy(
        update={"webhook_checks_per_minute": budget}
    )
    env.settings = env.settings.model_copy(
        update={
            "paypal_webhook_id": "WEBHOOK-1",
            "paypal_execution": "sandbox",
            "limits": limits,
        }
    )
    env.client = TestClient(create_app(env.settings, env.paypal))
    env.paypal.set_webhook_verification("SUCCESS")
    calls: list[str] = []
    real = env.paypal.verify_webhook

    async def counted(headers: Any, event: Any, webhook_id: str) -> bool:
        calls.append(event["id"])
        return await real(headers, event, webhook_id)

    env.paypal.verify_webhook = counted  # type: ignore[method-assign]
    return calls


def _post(env: Env, event_id: str, cert: str | None = None) -> int:
    headers = dict(SIGNED) if cert is None else {**SIGNED, CERT: cert}
    answer = env.client.post(
        "/v1/webhooks/paypal", json=_event(event_id), headers=headers
    )
    return answer.status_code


@pytest.mark.edge
@pytest.mark.parametrize(
    "cert",
    [
        "https://api.sandbox.paypal.com/v1/notifications/certs/CERT-1",
        "https://api.paypal.com/v1/notifications/certs/CERT-1",
    ],
)
def test_paypals_own_certificate_hosts_are_verified(tmp_path: Path, cert: str) -> None:
    env = Env(tmp_path)
    calls = _app(env)
    with env.client:
        assert _post(env, "WH-1", cert) == 200
    assert calls == ["WH-1"]


@pytest.mark.edge
@pytest.mark.parametrize(
    "cert",
    [
        "http://api.paypal.com/cert",
        "https://evil.example/cert",
        "https://paypal.com.evil.example/cert",
        "https://notpaypal.com/cert",
        "https://paypal.com@evil.example/cert",
        "not a url",
    ],
)
def test_a_certificate_not_from_paypal_never_reaches_paypal(
    tmp_path: Path, cert: str
) -> None:
    env = Env(tmp_path)
    calls = _app(env)
    with env.client:
        assert _post(env, "WH-1", cert) == 400
    assert calls == []


@pytest.mark.edge
def test_past_the_budget_paypal_is_not_called_and_is_asked_to_retry(
    tmp_path: Path,
) -> None:
    env = Env(tmp_path)
    calls = _app(env, budget=2)
    with env.client:
        statuses = [_post(env, f"WH-{n}") for n in range(3)]
    assert statuses == [200, 200, 429]
    assert calls == ["WH-0", "WH-1"]


@pytest.mark.edge
def test_a_refused_fake_spends_no_budget(tmp_path: Path) -> None:
    env = Env(tmp_path)
    calls = _app(env, budget=1)
    with env.client:
        assert _post(env, "WH-1", "https://evil.example/cert") == 400
        assert _post(env, "WH-2") == 200
    assert calls == ["WH-2"]


@pytest.mark.edge
def test_the_budget_comes_back_the_next_minute(tmp_path: Path) -> None:
    env = Env(tmp_path)
    limits = env.settings.limits.model_copy(update={"webhook_checks_per_minute": 1})
    guard = TrafficGuard(limits)
    assert guard.allow_webhook_check(0) is True
    assert guard.allow_webhook_check(59_999) is False
    assert guard.allow_webhook_check(60_000) is True
