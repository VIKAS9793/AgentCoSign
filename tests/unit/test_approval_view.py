"""What the approver is told: reasons, safety-check status and money left."""

import re

import pytest

from agentcosign.core.config import get_settings
from agentcosign.services.approval_view import (
    ONLY_SAFETY_REASON,
    SAFETY_PASSED,
    SAFETY_UNAVAILABLE,
    business_reasons,
    left_after,
    money_view,
    safety_check,
)

POLICY = get_settings().policy
LIMIT_LINE = f"Refunds over ${POLICY.refund_auto_limit:,.2f} need your OK."


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
    assert reasons[0].startswith("This customer has reached your limit")
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
