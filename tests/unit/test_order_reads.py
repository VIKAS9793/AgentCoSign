"""Reading PayPal orders for the merchant's screens.

Edge cases, written before the tests:

| Case | Expected |
|---|---|
| Ten cards on one order | one PayPal read |
| Same screen again soon, nothing changed | no read; first read time kept |
| One of our records changed | read again at once |
| Reuse time passed (boundary: exactly at it) | read again |
| A read that failed | never reused; the next screen reads again |
| Many orders at once | never more reads at once than the limit |
| An order with no id | no read, shown as not read |
"""

import asyncio
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest

from agentcosign.clients.paypal import RecordedPayPalClient
from agentcosign.core.exceptions import PayPalClientError
from agentcosign.services.order_reads import OrderReads
from tests.unit.paypal_fixtures import order_json

START = datetime(2026, 10, 13, 9, 0, tzinfo=UTC)


class CountingPayPal(RecordedPayPalClient):
    """Counts order reads and the most running at once."""

    def __init__(self, orders: dict[str, Any]) -> None:
        super().__init__(orders)
        self.reads = 0
        self.running = 0
        self.most_at_once = 0

    async def get_order(self, order_id: str) -> Any:
        self.reads += 1
        self.running += 1
        self.most_at_once = max(self.most_at_once, self.running)
        try:
            await asyncio.sleep(0.01)
            return await super().get_order(order_id)
        finally:
            self.running -= 1


class Clock:
    def __init__(self) -> None:
        self.now = START

    def __call__(self) -> datetime:
        return self.now


def _orders(*ids: str) -> dict[str, Any]:
    return {i: order_json(order=i, captured="500.00") for i in ids}


def _reader(paypal: CountingPayPal, clock: Clock, parallel: int = 4) -> OrderReads:
    return OrderReads(paypal, clock, max_parallel=parallel, reuse_seconds=10)


@pytest.mark.edge
async def test_many_cards_on_one_order_read_it_once() -> None:
    paypal = CountingPayPal(_orders("ORD-1"))
    reads = await _reader(paypal, Clock()).read_many(["ORD-1"] * 10, version=1)
    assert paypal.reads == 1
    assert reads["ORD-1"].facts["paid"] == "500.00"
    assert reads["ORD-1"].at == START


@pytest.mark.edge
async def test_a_read_is_reused_only_while_nothing_changed_and_time_is_left() -> None:
    paypal, clock = CountingPayPal(_orders("ORD-1")), Clock()
    reader = _reader(paypal, clock)
    await reader.read_many(["ORD-1"], version=1)
    clock.now = START + timedelta(seconds=9)
    again = await reader.read_many(["ORD-1"], version=1)
    assert paypal.reads == 1
    assert again["ORD-1"].at == START  # the time PayPal was really read
    await reader.read_many(["ORD-1"], version=2)  # one of our records changed
    assert paypal.reads == 2
    clock.now = START + timedelta(seconds=9 + 10)  # exactly the reuse time later
    await reader.read_many(["ORD-1"], version=2)
    assert paypal.reads == 3


@pytest.mark.edge
async def test_a_failed_read_is_never_reused() -> None:
    paypal, clock = CountingPayPal({}), Clock()
    reader = _reader(paypal, clock)
    failed = await reader.read_many(["ORD-1"], version=1)
    assert failed["ORD-1"].at is None and not failed["ORD-1"].facts["paid"]
    paypal._orders.update(_orders("ORD-1"))  # PayPal answers again
    found = await reader.read_many(["ORD-1"], version=1)
    assert paypal.reads == 2 and found["ORD-1"].at == START


@pytest.mark.edge
async def test_never_more_reads_at_once_than_the_limit() -> None:
    ids = [f"ORD-{n}" for n in range(9)]
    paypal = CountingPayPal(_orders(*ids))
    reads = await _reader(paypal, Clock(), parallel=3).read_many(ids, version=1)
    assert paypal.reads == 9 and paypal.most_at_once == 3
    assert all(r.at == START for r in reads.values())


@pytest.mark.edge
async def test_an_order_with_no_id_is_not_read() -> None:
    paypal = CountingPayPal({})
    reads = await _reader(paypal, Clock()).read_many([""], version=1)
    assert paypal.reads == 0 and reads[""].at is None


@pytest.mark.edge
async def test_an_unexpected_paypal_failure_is_shown_as_not_read() -> None:
    class Broken(CountingPayPal):
        async def get_order(self, order_id: str) -> Any:
            raise PayPalClientError("unreachable")

    reads = await _reader(Broken({}), Clock()).read_many(["ORD-1"], version=1)
    assert reads["ORD-1"].at is None


@pytest.mark.edge
async def test_a_read_in_flight_never_brings_back_dropped_figures() -> None:
    paypal = CountingPayPal(_orders("ORD-1"))
    reads = OrderReads(paypal, Clock(), max_parallel=4, reuse_seconds=10)
    in_flight = asyncio.create_task(reads.read_many(["ORD-1"], 1))
    await asyncio.sleep(0)  # the read has started
    reads.forget("ORD-1")  # the figures were found to be old meanwhile
    await in_flight
    await reads.read_many(["ORD-1"], 1)
    assert paypal.reads == 2  # the second screen read PayPal again
