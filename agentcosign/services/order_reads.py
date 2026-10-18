"""Reads PayPal orders for the merchant's screens, and only for display.

Each order is read once per screen, a few at a time, and a successful read is
reused for a short time while none of our own records changed. Every decision
and every refund reads PayPal again on its own, so nothing here can move money.
"""

import asyncio
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from datetime import datetime, timedelta

from agentcosign.clients.paypal import PayPalClient
from agentcosign.core.exceptions import PayPalClientError
from agentcosign.services.approval_view import order_facts, unavailable_facts


@dataclass(frozen=True, slots=True)
class Read:
    """An order's facts for display, and when PayPal was read (None if it was not)."""

    facts: dict[str, str]
    at: datetime | None


@dataclass(frozen=True, slots=True)
class _Kept:
    read: Read
    version: int


class OrderReads:
    """Display reads of PayPal orders, deduplicated, capped and briefly reused."""

    def __init__(
        self,
        paypal: PayPalClient,
        clock: Callable[[], datetime],
        *,
        max_parallel: int,
        reuse_seconds: int,
    ) -> None:
        self._paypal = paypal
        self._clock = clock
        self._max_parallel = max_parallel
        self._reuse = timedelta(seconds=reuse_seconds)
        self._kept: dict[str, _Kept] = {}

    async def read_many(
        self, order_ids: Iterable[str], version: int
    ) -> dict[str, Read]:
        """Facts for every order, reading each one PayPal has not shown recently.

        ``version`` grows with every change to our records; a read kept from an
        earlier version is never reused, so our own refunds show at once.
        """
        wanted = list(dict.fromkeys(order_ids))
        now = self._clock()
        found = {o: kept for o in wanted if (kept := self._reusable(o, version, now))}
        missing = [o for o in wanted if o not in found]
        gate = asyncio.Semaphore(self._max_parallel)
        fresh = await asyncio.gather(*(self._read(o, gate) for o in missing))
        for order_id, read in zip(missing, fresh, strict=True):
            found[order_id] = read
            if read.at is not None:
                self._kept[order_id] = _Kept(read, version)
        return found

    def forget(self, order_id: str) -> None:
        """Drop a kept read, so the next screen reads PayPal again."""
        self._kept.pop(order_id, None)

    def _reusable(self, order_id: str, version: int, now: datetime) -> Read | None:
        kept = self._kept.get(order_id)
        if kept is None or kept.version != version or kept.read.at is None:
            return None
        return kept.read if now - kept.read.at < self._reuse else None

    async def _read(self, order_id: str, gate: asyncio.Semaphore) -> Read:
        if not order_id:
            return Read(unavailable_facts(), None)
        async with gate:
            try:
                order = await self._paypal.get_order(order_id)
            except PayPalClientError:
                return Read(unavailable_facts(), None)
        return Read(order_facts(order), self._clock())
