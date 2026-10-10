"""Mapping PayPal records to evidence: only facts, and refuse anything unclear."""

from datetime import timedelta
from decimal import Decimal

import pytest

from agentcosign.core.exceptions import PayPalClientError
from agentcosign.services.evidence import order_evidence_from
from agentcosign.services.identity import card_fingerprint
from tests.scenarios.policy_scenarios import NOW
from tests.unit.paypal_fixtures import ACCOUNT, dispute_json, order_json

SECRET = b"s" * 32


def evidence(order: dict, disputes: list | None = None):  # noqa: ANN001,ANN201
    return order_evidence_from(order, disputes or [], fetched_at=NOW, secret=SECRET)


def test_basic_facts_come_from_the_order() -> None:
    ev = evidence(order_json())
    assert ev.order_id == "ORD-1"
    assert ev.capture_id == "CAP-1"
    assert ev.captured_amount == Decimal("100.00")
    assert ev.refunded_amount == Decimal("0")
    assert ev.account_id == ACCOUNT
    assert ev.currency == "USD"
    assert ev.has_open_dispute is False
    assert ev.fetched_at == NOW


def test_completed_and_pending_refunds_count_failed_ones_do_not() -> None:
    order = order_json(
        refunds=(("10.00", "COMPLETED"), ("5.00", "PENDING"), ("50.00", "FAILED"))
    )
    assert evidence(order).refunded_amount == Decimal("15.00")


def test_open_dispute_is_reported_resolved_one_is_not() -> None:
    open_case = evidence(
        order_json(), [dispute_json(status="RESOLVED"), dispute_json("DSP-2")]
    )
    assert open_case.has_open_dispute is True
    assert open_case.dispute_id == "DSP-2"
    assert open_case.dispute_amount == Decimal("40.00")
    closed = evidence(order_json(), [dispute_json(status="RESOLVED")])
    assert closed.has_open_dispute is False


def test_card_payments_get_a_customer_fingerprint_wallet_payments_do_not() -> None:
    assert evidence(order_json()).customer_key == card_fingerprint(
        SECRET, "VISA", "4242", "Jane Buyer"
    )
    assert evidence(order_json(card=False)).customer_key is None


@pytest.mark.parametrize(
    "broken",
    [
        {**order_json(), "purchase_units": []},
        {**order_json(), "purchase_units": order_json()["purchase_units"] * 2},
        order_json(merchant=None),
        {**order_json(), "id": ""},
    ],
    ids=["no-unit", "two-units", "no-merchant", "no-id"],
)
def test_ambiguous_or_incomplete_orders_are_refused(broken: dict) -> None:
    with pytest.raises(PayPalClientError):
        evidence(broken)


def test_refund_in_another_currency_is_refused() -> None:
    order = order_json()
    order["purchase_units"][0]["payments"]["refunds"] = [
        {"status": "COMPLETED", "amount": {"value": "1.00", "currency_code": "EUR"}}
    ]
    with pytest.raises(PayPalClientError):
        evidence(order)


def test_unreadable_amounts_are_refused() -> None:
    for value in ("NaN", "abc", "Infinity"):
        with pytest.raises(PayPalClientError):
            evidence(order_json(captured=value))


def test_two_captures_are_refused() -> None:
    order = order_json()
    captures = order["purchase_units"][0]["payments"]["captures"]
    captures.append({**captures[0], "id": "CAP-2"})
    with pytest.raises(PayPalClientError):
        evidence(order)


def test_the_fetch_time_is_the_clock_passed_in() -> None:
    later = NOW + timedelta(minutes=5)
    ev = order_evidence_from(order_json(), [], fetched_at=later, secret=SECRET)
    assert ev.fetched_at == later
