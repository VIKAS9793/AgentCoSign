"""Reconciliation: what we sent compared with what PayPal reports."""

from datetime import datetime, timedelta
from decimal import Decimal

import pytest

from agentcosign.services.reconciliation import (
    Known,
    OurRefund,
    Report,
    compare,
    paypal_refunds,
)
from tests.scenarios.policy_scenarios import NOW

STALE = timedelta(hours=24)
SINCE = NOW - timedelta(days=7)
NOTHING_ELSE = Known(counted=frozenset(), not_sent=frozenset(), paypal_ids=frozenset())


def check(
    our: list[OurRefund],
    orders: dict[str, dict[str, object] | None],
    known: Known = NOTHING_ELSE,
) -> Report:
    return compare(our, orders, NOW, STALE, since=SINCE, known=known)


def ours(
    request_id: str = "r1", amount: str = "10.00", sent_at: datetime | None = None
) -> OurRefund:
    return OurRefund(
        request_id=request_id,
        order_id="ORD-1",
        amount=Decimal(amount),
        paypal_id=f"PP-{request_id}",
        sent_at=sent_at or NOW - timedelta(hours=1),
    )


def order(*refunds: dict[str, object]) -> dict[str, object]:
    return {"purchase_units": [{"payments": {"refunds": list(refunds)}}]}


def refund(
    refund_id: str,
    amount: str,
    status: str = "COMPLETED",
    stamp: str | None = None,
    created: str | None = None,
) -> dict[str, object]:
    item: dict[str, object] = {
        "id": refund_id,
        "status": status,
        "amount": {"value": amount, "currency_code": "USD"},
    }
    if created is not None:
        item["create_time"] = created
    if stamp is not None:
        item["custom_id"] = stamp
    return item


def test_every_refund_found_complete_and_equal_is_clean() -> None:
    report = check([ours()], {"ORD-1": order(refund("PP-r1", "10.00", stamp="r1"))})
    assert report.matched == 1 and not report.breaks and not report.outside
    assert not report.unchecked


@pytest.mark.edge
def test_a_refund_we_sent_that_paypal_does_not_have_is_a_break() -> None:
    report = check([ours()], {"ORD-1": order()})
    assert [(b.request_id, b.kind) for b in report.breaks] == [("r1", "missing")]


@pytest.mark.edge
def test_a_different_amount_at_paypal_is_a_break() -> None:
    theirs = {"ORD-1": order(refund("PP-r1", "9.99", stamp="r1"))}
    report = check([ours()], theirs)
    assert [b.kind for b in report.breaks] == ["amount_differs"]


@pytest.mark.edge
@pytest.mark.parametrize(
    ("hours_ago", "expected"), [(1, []), (25, ["processing_too_long"])]
)
def test_processing_becomes_a_break_only_after_the_allowed_time(
    hours_ago: int, expected: list[str]
) -> None:
    sent = NOW - timedelta(hours=hours_ago)
    theirs = {"ORD-1": order(refund("PP-r1", "10.00", "PENDING", stamp="r1"))}
    report = check([ours(sent_at=sent)], theirs)
    assert [b.kind for b in report.breaks] == expected
    assert report.processing == (1 if not expected else 0)


@pytest.mark.edge
def test_a_refund_paypal_failed_after_we_sent_it_is_a_break() -> None:
    theirs = {"ORD-1": order(refund("PP-r1", "10.00", "FAILED", stamp="r1"))}
    report = check([ours()], theirs)
    assert [b.kind for b in report.breaks] == ["failed_at_paypal"]


@pytest.mark.edge
def test_refunds_made_in_paypal_directly_are_reported_apart_from_our_breaks() -> None:
    theirs = {
        "ORD-1": order(
            refund("PP-r1", "10.00", stamp="r1"), refund("PP-outside", "47.00")
        )
    }
    report = check([ours()], theirs)
    assert report.matched == 1 and not report.breaks
    assert [(o.order_id, o.amount) for o in report.outside] == [
        ("ORD-1", Decimal("47.00"))
    ]


@pytest.mark.edge
def test_an_order_paypal_would_not_show_is_unchecked_never_clean() -> None:
    report = check([ours()], {"ORD-1": None})
    assert report.unchecked == ("ORD-1",)
    assert report.matched == 0 and not report.breaks


@pytest.mark.edge
def test_our_refund_is_recognised_by_its_paypal_id_when_the_stamp_is_missing() -> None:
    report = check([ours()], {"ORD-1": order(refund("PP-r1", "10.00"))})
    assert report.matched == 1 and not report.outside


@pytest.mark.edge
def test_an_unreadable_refund_record_makes_the_order_unchecked() -> None:
    assert (
        paypal_refunds({"purchase_units": [{"payments": {"refunds": ["x"]}}]}) is None
    )


# Edge cases: our own older or failed refunds, money that moved for a request we
# recorded as not sent, an order left out of the run, and outside refunds that
# are old, failed or carry no time.
OLD = (NOW - timedelta(days=30)).isoformat()
RECENT = (NOW - timedelta(hours=2)).isoformat()


@pytest.mark.edge
def test_our_older_refund_on_the_order_is_never_called_outside() -> None:
    known = Known(
        counted=frozenset({"r0"}), not_sent=frozenset(), paypal_ids=frozenset({"PP-x"})
    )
    theirs = {
        "ORD-1": order(
            refund("PP-r1", "10.00", stamp="r1"),
            # ours, still being sent, so we have no PayPal id for it yet
            refund("PP-r0", "5.00", stamp="r0", created=RECENT),
            refund("PP-x", "4.00"),  # ours from before refunds carried a stamp
        )
    }
    report = check([ours()], theirs, known)
    assert report.matched == 1 and not report.outside and not report.breaks


@pytest.mark.edge
def test_our_failed_request_that_paypal_also_failed_is_not_outside() -> None:
    known = Known(
        counted=frozenset(), not_sent=frozenset({"rF"}), paypal_ids=frozenset()
    )
    theirs = {"ORD-1": order(refund("PP-rF", "10.00", "FAILED", stamp="rF"))}
    report = check([], theirs, known)
    assert not report.outside and not report.breaks


@pytest.mark.edge
@pytest.mark.parametrize("status", ["COMPLETED", "PENDING"])
def test_money_moved_for_a_request_we_recorded_as_not_sent_is_a_break(
    status: str,
) -> None:
    known = Known(
        counted=frozenset(), not_sent=frozenset({"rF"}), paypal_ids=frozenset()
    )
    theirs = {"ORD-1": order(refund("PP-rF", "10.00", status, stamp="rF"))}
    report = check([], theirs, known)
    assert [(b.request_id, b.kind) for b in report.breaks] == [
        ("rF", "sent_without_our_record")
    ]
    assert not report.outside


@pytest.mark.edge
def test_a_refund_we_sent_on_an_order_left_out_of_the_run_is_unchecked() -> None:
    report = check([ours()], {})
    assert report.unchecked == ("ORD-1",)
    assert report.matched == 0


@pytest.mark.edge
@pytest.mark.parametrize(
    ("created", "status", "reported"),
    [
        (OLD, "COMPLETED", False),  # before the window: reported by an earlier run
        (RECENT, "COMPLETED", True),
        (RECENT, "PENDING", True),  # money is on its way out
        (RECENT, "FAILED", False),  # no money moved
        (RECENT, "CANCELLED", False),
        (None, "COMPLETED", True),  # no time from PayPal: never hidden
        ("not a time", "COMPLETED", True),
        ("2026-10-01T09:00:00", "COMPLETED", True),  # no time zone: unreadable
        (SINCE.isoformat(), "COMPLETED", True),  # exactly at the window start
    ],
)
def test_outside_refunds_are_reported_once_and_only_when_money_moved(
    created: str | None, status: str, reported: bool
) -> None:
    theirs = {"ORD-1": order(refund("PP-out", "47.00", status, created=created))}
    report = check([], theirs)
    assert bool(report.outside) is reported
