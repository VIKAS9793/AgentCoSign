"""The PayPal client: tokens, idempotency headers, error handling, safe ids."""

import json
from decimal import Decimal

import httpx
import pytest
from pydantic import SecretStr

from agentcosign.clients.paypal import (
    REQUEST_ID_HEADER,
    SANDBOX_BASE,
    LivePayPalClient,
    RecordedPayPalClient,
)
from agentcosign.core.exceptions import PayPalClientError
from tests.unit.paypal_fixtures import order_json

SECRET = SecretStr("sandbox-secret")


class FakePayPal:
    """Records requests and answers them from a script."""

    def __init__(self, responses: list[httpx.Response] | None = None) -> None:
        self.requests: list[httpx.Request] = []
        self.responses = responses or []
        self.token_calls = 0

    def handler(self, request: httpx.Request) -> httpx.Response:
        if request.url.path == "/v1/oauth2/token":
            self.token_calls += 1
            return httpx.Response(
                200, json={"access_token": f"T{self.token_calls}", "expires_in": 32400}
            )
        self.requests.append(request)
        if self.responses:
            return self.responses.pop(0)
        return httpx.Response(200, json={"id": "RF-1", "status": "COMPLETED"})


def client_for(
    fake: FakePayPal, clock_value: list[int] | None = None
) -> LivePayPalClient:
    clock = clock_value or [0]
    http = httpx.AsyncClient(
        base_url=SANDBOX_BASE, transport=httpx.MockTransport(fake.handler)
    )
    return LivePayPalClient("client-id", SECRET, http=http, clock=lambda: clock[0])


async def test_refund_sends_the_request_id_and_exact_amount() -> None:
    fake = FakePayPal()
    client = client_for(fake)
    result = await client.refund_capture("CAP-1", Decimal("12.50"), "USD", "req-123")
    request = fake.requests[0]
    assert result["id"] == "RF-1"
    assert request.headers[REQUEST_ID_HEADER] == "req-123"
    assert request.headers["Authorization"] == "Bearer T1"
    assert json.loads(request.content)["amount"] == {
        "value": "12.50",
        "currency_code": "USD",
    }
    assert request.url.path == "/v2/payments/captures/CAP-1/refund"
    body = json.loads(request.content)
    # stamped with our request id so we can recognise our own refund later
    assert body["custom_id"] == "req-123"
    assert body["invoice_id"] == "req-123"
    assert body["note_to_payer"]


async def test_token_is_cached_and_refreshed_before_expiry() -> None:
    fake = FakePayPal()
    clock = [0]
    client = client_for(fake, clock)
    await client.get_order("ORD-1")
    await client.get_order("ORD-1")
    assert fake.token_calls == 1
    clock[0] = 32_400_000  # past expiry minus the safety margin
    await client.get_order("ORD-1")
    assert fake.token_calls == 2


async def test_an_expired_token_is_replaced_once() -> None:
    fake = FakePayPal([httpx.Response(401), httpx.Response(200, json={"id": "ORD-1"})])
    client = client_for(fake)
    assert (await client.get_order("ORD-1"))["id"] == "ORD-1"
    assert fake.token_calls == 2


async def test_persistent_unauthorised_fails() -> None:
    fake = FakePayPal([httpx.Response(401), httpx.Response(401)])
    with pytest.raises(PayPalClientError):
        await client_for(fake).get_order("ORD-1")


async def test_a_refusal_is_summarised_and_not_retryable() -> None:
    body = {
        "name": "UNPROCESSABLE_ENTITY",
        "details": [{"issue": "REFUND_AMOUNT_EXCEEDED"}],
        "debug_id": "abc",
        "message": "long text that must not be kept",
    }
    fake = FakePayPal([httpx.Response(422, json=body)])
    with pytest.raises(PayPalClientError) as caught:
        await client_for(fake).refund_capture("CAP-1", Decimal("1.00"), "USD", "r")
    details = caught.value.details
    assert details["issues"] == ["REFUND_AMOUNT_EXCEEDED"]
    assert details["retryable"] is False
    assert "long text" not in str(details)


@pytest.mark.parametrize("status", [429, 500, 503])
async def test_server_side_trouble_is_retryable(status: int) -> None:
    fake = FakePayPal([httpx.Response(status)])
    with pytest.raises(PayPalClientError) as caught:
        await client_for(fake).refund_capture("CAP-1", Decimal("1.00"), "USD", "r")
    assert caught.value.details["retryable"] is True


async def test_network_failure_is_retryable() -> None:
    def broken(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/v1/oauth2/token":
            return httpx.Response(200, json={"access_token": "T", "expires_in": 100})
        raise httpx.ConnectTimeout("timed out", request=request)

    http = httpx.AsyncClient(
        base_url=SANDBOX_BASE, transport=httpx.MockTransport(broken)
    )
    client = LivePayPalClient("id", SECRET, http=http)
    with pytest.raises(PayPalClientError) as caught:
        await client.get_order("ORD-1")
    assert caught.value.details["retryable"] is True


@pytest.mark.parametrize(
    "bad", ["../../v1/oauth2", "ord-1", "A" * 65, "", "ORD 1", "ORD/1"]
)
async def test_ids_that_could_alter_the_url_are_refused_before_any_call(
    bad: str,
) -> None:
    fake = FakePayPal()
    client = client_for(fake)
    with pytest.raises(PayPalClientError):
        await client.get_order(bad)
    with pytest.raises(PayPalClientError):
        await client.refund_capture(bad, Decimal("1.00"), "USD", "r")
    assert fake.requests == []
    assert fake.token_calls == 0


def test_missing_credentials_are_refused() -> None:
    with pytest.raises(PayPalClientError):
        LivePayPalClient("", SECRET)


# --- recorded client ----------
async def test_recorded_refund_is_idempotent_and_visible_on_the_order() -> None:
    client = RecordedPayPalClient({"ORD-1": order_json()})
    first = await client.refund_capture("CAP-1", Decimal("10.00"), "USD", "req-1")
    again = await client.refund_capture("CAP-1", Decimal("10.00"), "USD", "req-1")
    assert first == again
    refunds = (await client.get_order("ORD-1"))["purchase_units"][0]["payments"][
        "refunds"
    ]
    assert len(refunds) == 1


async def test_recorded_unknown_order_is_not_found() -> None:
    client = RecordedPayPalClient({})
    with pytest.raises(PayPalClientError) as caught:
        await client.get_order("ORD-404")
    assert caught.value.details["status"] == 404


@pytest.mark.parametrize("bad", ["x" * 39, "req\r\nX-Evil: 1", "", "req id"])
async def test_request_ids_that_break_the_header_are_refused_before_any_call(
    bad: str,
) -> None:
    fake = FakePayPal()
    client = client_for(fake)
    with pytest.raises(PayPalClientError):
        await client.refund_capture("CAP-1", Decimal("1.00"), "USD", bad)
    with pytest.raises(PayPalClientError):
        await client.accept_claim("DSP-1", bad)
    recorded = RecordedPayPalClient({"ORD-1": order_json()})
    with pytest.raises(PayPalClientError):
        await recorded.refund_capture("CAP-1", Decimal("1.00"), "USD", bad)
    assert fake.requests == []
