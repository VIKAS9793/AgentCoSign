"""What the approver is told: reasons, safety-check status and money left."""

import re
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest

from agentcosign.core.config import get_settings
from agentcosign.services import approval_view
from agentcosign.services.approval_view import (
    ONLY_SAFETY_REASON,
    SAFETY_PASSED,
    SAFETY_UNAVAILABLE,
    business_reasons,
    checked_time,
    left_after,
    money_view,
    paypal_checked,
    read_mark,
    safety_check,
    scan_note,
)

POLICY = get_settings().policy
LIMIT_LINE = f"Refunds over ${POLICY.refund_auto_limit:,.2f} need your approval."


def test_a_check_that_did_not_run_is_never_reported_as_passed() -> None:
    assert safety_check(["R15"]) == SAFETY_UNAVAILABLE
    assert safety_check(["R2", "R15"]) == SAFETY_UNAVAILABLE
    assert safety_check(["R2"]) == SAFETY_PASSED


def test_the_safety_outage_does_not_hide_the_business_reason() -> None:
    reasons = business_reasons(["R15", "R2"], POLICY)
    assert reasons == [LIMIT_LINE]


def test_an_outage_alone_says_the_assistant_could_usually_send_it() -> None:
    assert business_reasons(["R15"], POLICY) == [ONLY_SAFETY_REASON]


def test_every_reason_is_shown_most_serious_first() -> None:
    reasons = business_reasons(["R2", "R5", "R15"], POLICY)
    assert len(reasons) == 2
    assert reasons[0].startswith("This would take this customer past your limit")
    assert reasons[1] == LIMIT_LINE


def test_no_rule_codes_reach_the_owner() -> None:
    for triggered in (["R2"], ["R5"], ["R6"], ["R7"], ["R8"], ["R14"], ["R15"]):
        for line in business_reasons(triggered, POLICY):
            assert not re.search(r"R\d+", line)


@pytest.mark.parametrize(
    ("paid", "refunded", "amount", "expected"),
    [
        ("50.00", "35.00", "10.00", "5.00"),
        ("50.00", "0.00", "50.00", "0.00"),
        ("0.30", "0.10", "0.10", "0.10"),
    ],
)
def test_left_to_refund_is_exact(
    paid: str, refunded: str, amount: str, expected: str
) -> None:
    facts = {"paid": paid, "already_refunded": refunded}
    assert left_after(facts, amount) == expected


def test_left_to_refund_is_blank_when_paypal_was_unreachable() -> None:
    assert left_after({"paid": "", "already_refunded": ""}, "10.00") == ""


def test_the_screen_shows_what_can_be_refunded_now_and_after() -> None:
    facts = {"paid": "50.00", "already_refunded": "0.00"}
    assert money_view(facts, "30.00") == {
        "refundable_now": "50.00",
        "left_after": "20.00",
        "fits": "yes",
        "waiting_on_order": "0.00",
    }


@pytest.mark.edge
def test_a_refund_that_no_longer_fits_is_marked_so_no_send_is_offered() -> None:
    # someone refunded 35.00 in PayPal directly while a 30.00 request waited
    facts = {"paid": "50.00", "already_refunded": "35.00"}
    view = money_view(facts, "30.00")
    assert view["fits"] == "no"
    assert view["refundable_now"] == "15.00"


@pytest.mark.edge
def test_refunding_exactly_what_is_left_fits() -> None:
    facts = {"paid": "50.00", "already_refunded": "20.00"}
    assert money_view(facts, "30.00")["fits"] == "yes"
    assert money_view(facts, "30.01")["fits"] == "no"


@pytest.mark.edge
def test_unknown_amounts_are_never_guessed() -> None:
    assert money_view({"paid": "", "already_refunded": ""}, "10.00")["fits"] == (
        "unknown"
    )


@pytest.mark.edge
def test_other_refunds_waiting_on_the_order_are_counted_as_promised() -> None:
    # found on the phone: two $1 cards each said $4 left; approving both leaves $3
    facts = {"paid": "50.00", "already_refunded": "45.00"}
    view = money_view(facts, "1.00", Decimal("1.00"))
    assert view["refundable_now"] == "4.00"
    assert view["left_after"] == "3.00"
    assert money_view(facts, "4.50", Decimal("1.00"))["fits"] == "no"


@pytest.mark.parametrize(
    ("triggered", "expected"),
    [
        (["R15"], ""),  # the scan is already the only reason given
        (["R2", "R15"], "The safety check on the customer's message didn't run."),
        (["R2"], ""),  # the scan ran
    ],
)
@pytest.mark.edge
def test_the_scan_note_appears_only_beside_other_reasons(
    triggered: list[str], expected: str
) -> None:
    assert scan_note(triggered, POLICY) == expected


READ_AT = datetime(2026, 10, 13, 9, 0, tzinfo=UTC)
READ = {"paypal_read": "yes", "checked_at": READ_AT.isoformat()}
FAILED = {"paypal_read": "no", "checked_at": ""}
NOT_TRIED = {"paypal_read": "", "checked_at": ""}


@pytest.mark.parametrize(
    ("views", "expected"),
    [
        ([], "none"),
        ([NOT_TRIED], "none"),
        ([READ, NOT_TRIED], "yes"),
        ([READ, FAILED], "partial"),
        ([FAILED, NOT_TRIED], "no"),
    ],
)
@pytest.mark.edge
def test_paypal_is_called_checked_only_for_cards_really_read(
    views: list[dict[str, str]], expected: str
) -> None:
    assert paypal_checked(views) == expected


@pytest.mark.edge
def test_only_a_successful_read_carries_a_check_time() -> None:
    assert read_mark({"paid": "500.00"}, READ_AT) == READ
    assert read_mark({"paid": ""}, READ_AT) == FAILED


@pytest.mark.edge
def test_the_home_check_time_is_the_oldest_read_and_only_when_all_were_read() -> None:
    later = {"paypal_read": "yes", "checked_at": "2026-10-13T09:05:00+00:00"}
    assert checked_time([later, READ], "yes") == READ["checked_at"]
    assert checked_time([READ, FAILED], "partial") == ""
    assert checked_time([], "none") == ""


@pytest.mark.edge
def test_the_on_hold_history_title_follows_the_rule_window(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(approval_view, "VELOCITY_WINDOW", timedelta(hours=12))
    assert "12-hour" in approval_view.outcome_title("paused")
    assert approval_view.outcome_title("unknown") == ""


@pytest.mark.edge
def test_the_automatic_budget_reason_reads_in_owner_words() -> None:
    policy = get_settings().policy
    words = approval_view.plain_reason("R17", policy)
    assert words != approval_view.plain_reason("", policy)  # never the generic line
    assert "without asking" in words
    assert f"${policy.refund_auto_limit:,.2f}" in words  # the configured limit
    assert not re.search(r"\bR\d+\b|agent|escalat", words, re.IGNORECASE)
    assert (
        approval_view.short_flag(["R17"], policy) == "Already refunded without asking"
    )
