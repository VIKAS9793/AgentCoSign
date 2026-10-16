"""Reconciliation: compare the refunds we sent with what PayPal reports.

Pure functions only: the gateway fetches PayPal's orders and records the
result. An order PayPal would not show is reported as unchecked, never as clean.
"""

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta
from decimal import Decimal, InvalidOperation
from typing import Any

COMPLETED = "COMPLETED"
STILL_PROCESSING = frozenset({"PENDING"})
# statuses where money has left or is leaving the merchant's account
MONEY_MOVED = frozenset({COMPLETED}) | STILL_PROCESSING


@dataclass(frozen=True, slots=True)
class OurRefund:
    """A refund this service sent, as its own record shows it."""

    request_id: str
    order_id: str
    amount: Decimal
    paypal_id: str
    sent_at: datetime


@dataclass(frozen=True, slots=True)
class PayPalRefund:
    """One refund as PayPal reports it on the order."""

    refund_id: str
    amount: Decimal
    status: str
    stamp: str
    created: datetime | None = None


@dataclass(frozen=True, slots=True)
class Known:
    """Every refund request this service has made on the orders being checked.

    ``counted`` were sent or are being sent, ``not_sent`` ended any other way,
    and ``paypal_ids`` are the PayPal refund ids we recorded, which recognise our
    refunds made before each refund carried our request id.
    """

    counted: frozenset[str]
    not_sent: frozenset[str]
    paypal_ids: frozenset[str]


@dataclass(frozen=True, slots=True)
class Break:
    """A refund we sent that PayPal does not show the way we recorded it."""

    request_id: str
    order_id: str
    # missing, amount_differs, failed_at_paypal, processing_too_long, or
    # sent_without_our_record: PayPal moved money for a request we recorded as
    # not sent
    kind: str


@dataclass(frozen=True, slots=True)
class OutsideRefund:
    """A refund on one of our orders that this service did not send."""

    order_id: str
    refund_id: str
    amount: Decimal


@dataclass(frozen=True, slots=True)
class Report:
    """The result of one reconciliation run."""

    matched: int
    processing: int
    breaks: tuple[Break, ...]
    outside: tuple[OutsideRefund, ...]
    unchecked: tuple[str, ...]

    def summary(self) -> dict[str, int]:
        """Counts only, safe to record in the chain and show the merchant."""
        return {
            "matched": self.matched,
            "processing": self.processing,
            "breaks": len(self.breaks),
            "outside": len(self.outside),
            "unchecked": len(self.unchecked),
        }


def paypal_refunds(order: Mapping[str, Any]) -> list[PayPalRefund] | None:
    """Every refund on a PayPal order, or None if the record is not readable."""
    found: list[PayPalRefund] = []
    try:
        for unit in order.get("purchase_units") or []:
            for item in (unit.get("payments") or {}).get("refunds") or []:
                if not isinstance(item, Mapping):
                    return None
                amount = Decimal(str((item.get("amount") or {}).get("value")))
                stamp = item.get("custom_id")
                found.append(
                    PayPalRefund(
                        refund_id=str(item.get("id", "")),
                        amount=amount,
                        status=str(item.get("status", "")),
                        stamp=stamp if isinstance(stamp, str) else "",
                        created=_created(item.get("create_time")),
                    )
                )
    except (AttributeError, TypeError, InvalidOperation):
        return None
    return found


def _created(value: object) -> datetime | None:
    """PayPal's creation time, or None when it is missing or unreadable."""
    if not isinstance(value, str):
        return None
    try:
        created = datetime.fromisoformat(value)
    except ValueError:
        return None
    return created if created.tzinfo is not None else None


def _ours_at_paypal(
    refund: OurRefund, theirs: Sequence[PayPalRefund]
) -> PayPalRefund | None:
    for item in theirs:
        if item.stamp == refund.request_id or (
            refund.paypal_id and item.refund_id == refund.paypal_id
        ):
            return item
    return None


def _unmatched(
    item: PayPalRefund, order_id: str, known: Known, since: datetime
) -> Break | OutsideRefund | None:
    """What a PayPal refund not matched to one of this run's refunds means.

    A failed or cancelled refund moved no money. Our own refunds, older or still
    being sent, are already counted. Money that moved for a request we recorded
    as not sent is a break. Anything else is a refund made in PayPal directly,
    reported once: those older than this run's window were reported before, and
    one without a readable time is reported rather than hidden.
    """
    if item.status not in MONEY_MOVED:
        return None
    if item.stamp in known.not_sent:
        return Break(item.stamp, order_id, "sent_without_our_record")
    if item.stamp in known.counted or item.refund_id in known.paypal_ids:
        return None
    if item.created is not None and item.created < since:
        return None
    return OutsideRefund(order_id, item.refund_id, item.amount)


def _verdict(
    refund: OurRefund, found: PayPalRefund | None, now: datetime, stale_after: timedelta
) -> str:
    """matched, processing, or the kind of break for one refund we sent."""
    if found is None:
        return "missing"
    if found.amount != refund.amount:
        return "amount_differs"
    if found.status == COMPLETED:
        return "matched"
    if found.status in STILL_PROCESSING:
        stale = now - refund.sent_at > stale_after
        return "processing_too_long" if stale else "processing"
    return "failed_at_paypal"


@dataclass(frozen=True, slots=True)
class _Run:
    """The settings of one reconciliation run."""

    now: datetime
    stale_after: timedelta
    since: datetime
    known: Known

    def check_order(
        self, order_id: str, ours: Sequence[OurRefund], theirs: Sequence[PayPalRefund]
    ) -> tuple[list[str], list[Break], list[OutsideRefund]]:
        """Verdicts for our refunds on one order, plus what PayPal shows apart."""
        verdicts: list[str] = []
        breaks: list[Break] = []
        outside: list[OutsideRefund] = []
        recognised: set[str] = set()
        for refund in ours:
            found = _ours_at_paypal(refund, theirs)
            if found is not None:
                recognised.add(found.refund_id)
            verdict = _verdict(refund, found, self.now, self.stale_after)
            verdicts.append(verdict)
            if verdict not in ("matched", "processing"):
                breaks.append(Break(refund.request_id, order_id, verdict))
        for item in (i for i in theirs if i.refund_id not in recognised):
            match _unmatched(item, order_id, self.known, self.since):
                case Break() as found_break:
                    breaks.append(found_break)
                case OutsideRefund() as found_outside:
                    outside.append(found_outside)
        return verdicts, breaks, outside


def compare(
    ours: Sequence[OurRefund],
    orders: Mapping[str, Mapping[str, Any] | None],
    now: datetime,
    stale_after: timedelta,
    *,
    since: datetime,
    known: Known,
) -> Report:
    """Match each refund we sent to PayPal's record of the same order.

    ``orders`` maps every order to check to PayPal's record, or None when PayPal
    could not be read for it. A refund we sent on an order missing from it is
    reported as unchecked, never dropped.
    """
    run = _Run(now, stale_after, since, known)
    verdicts: list[str] = []
    breaks: list[Break] = []
    outside: list[OutsideRefund] = []
    unchecked: list[str] = sorted({r.order_id for r in ours} - set(orders))
    for order_id, record in orders.items():
        theirs = paypal_refunds(record) if record is not None else None
        if theirs is None:
            unchecked.append(order_id)
            continue
        on_order = [r for r in ours if r.order_id == order_id]
        order_verdicts, order_breaks, order_outside = run.check_order(
            order_id, on_order, theirs
        )
        verdicts += order_verdicts
        breaks += order_breaks
        outside += order_outside
    return Report(
        verdicts.count("matched"),
        verdicts.count("processing"),
        tuple(breaks),
        tuple(outside),
        tuple(unchecked),
    )
