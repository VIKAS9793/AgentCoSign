"""A refund goes out alone only when the customer's own words ask for exactly it.

Edge cases (written before the code):

| Case | Expected |
|---|---|
| Reading matches order, amount, currency; ordinary reason | Allowed |
| "10" asked, 10.00 proposed | Allowed (same amount) |
| Different order, no order, different amount, no amount | Merchant |
| Different currency; money asked to go elsewhere | Merchant |
| Charged twice, earlier refund missing, unrecognised charge, changed mind | Merchant |
| No clear reason, or no reason read | Merchant |
| Each plain reason | Allowed |
| A message whose order is not known | Merchant, never alone |
| Message could not be read; marked read but nothing came back | Merchant |
| A reading on its own | Never refuses |
| No customer message tied to the request | No reading check |
| The approval card | Plain words, no codes, its own line |
"""

import re
from dataclasses import replace
from decimal import Decimal
from typing import Any

import pytest

from agentcosign.models.domain import Decision
from agentcosign.models.policy import CustomerReading, ReadingStatus, Signals
from agentcosign.services.approval_view import (
    UNREAD_MESSAGE,
    business_reasons,
    customer_asked,
    plain_reason,
    short_flag,
)
from agentcosign.services.policy_engine import RULE_CUSTOMER_WORDS, evaluate
from tests.scenarios.policy_scenarios import NOW, Scenario, policy

BASE = Scenario("T", "clean small refund", Decision.ALLOW, "R1")
MATCH = CustomerReading("ORD-1", Decimal("10.00"), "USD", "not_received")

MISMATCHES = [
    {"order_id": "ORD-OTHER"},
    {"order_id": None},
    {"amount": Decimal("10.01")},
    {"amount": None},
    {"currency": "INR"},
    {"wants_other_destination": True},
    {"reason": "duplicate_charge"},
    {"reason": "refund_not_received"},
    {"reason": "unrecognised_charge"},
    {"reason": "changed_mind"},
    {"reason": "other"},
    {"reason": None},
]
PLAIN = (
    "not_received",
    "damaged",
    "not_as_described",
    "wrong_item",
    "subscription_cancelled",
)


def _decide(signals: Signals, **proposal: Any) -> tuple[Decision, tuple[str, ...]]:
    built, evidence, ledger, _, rules, screening = BASE.build()
    built = replace(built, **proposal) if proposal else built
    result = evaluate(built, evidence, ledger, signals, rules, screening, NOW)
    return result.decision, result.triggered_rules


def _read(reading: CustomerReading | None) -> Signals:
    return Signals(
        reading_status=ReadingStatus.READ, reading=reading, message_order="ORD-1"
    )


@pytest.mark.edge
@pytest.mark.parametrize("reason", PLAIN)
def test_each_plain_reason_may_go_alone(reason: str) -> None:
    decision, _ = _decide(_read(replace(MATCH, reason=reason)))
    assert decision is Decision.ALLOW


@pytest.mark.edge
def test_a_message_whose_order_is_not_known_never_goes_alone() -> None:
    signals = replace(_read(MATCH), message_order=None)
    decision, rules = _decide(signals)
    assert decision is Decision.ESCALATE
    assert RULE_CUSTOMER_WORDS in rules


@pytest.mark.edge
@pytest.mark.parametrize("amount", [Decimal("10.00"), Decimal("10")])
def test_words_that_match_let_a_small_refund_go_alone(amount: Decimal) -> None:
    decision, _ = _decide(_read(replace(MATCH, amount=amount)))
    assert decision is Decision.ALLOW


@pytest.mark.edge
@pytest.mark.parametrize("change", MISMATCHES)
def test_any_gap_between_words_and_refund_asks_the_merchant(
    change: dict[str, Any],
) -> None:
    decision, triggered = _decide(_read(replace(MATCH, **change)))
    assert decision is Decision.ESCALATE
    assert RULE_CUSTOMER_WORDS in triggered


# the reading failed, or was marked read with nothing in it
UNREAD = [Signals(reading_status=ReadingStatus.UNAVAILABLE), _read(None)]


@pytest.mark.edge
@pytest.mark.parametrize("signals", UNREAD)
def test_an_unread_message_asks_the_merchant(signals: Signals) -> None:
    decision, triggered = _decide(signals)
    assert decision is Decision.ESCALATE
    assert RULE_CUSTOMER_WORDS in triggered


@pytest.mark.edge
@pytest.mark.parametrize("change", MISMATCHES)
def test_a_reading_alone_never_refuses(change: dict[str, Any]) -> None:
    decision, _ = _decide(_read(replace(MATCH, **change)))
    assert decision is not Decision.DENY


def test_without_a_customer_message_there_is_nothing_to_compare() -> None:
    decision, triggered = _decide(Signals())
    assert decision is Decision.ALLOW
    assert RULE_CUSTOMER_WORDS not in triggered


@pytest.mark.edge
def test_the_card_explains_it_in_the_owners_words() -> None:
    rules = policy()
    line = plain_reason(RULE_CUSTOMER_WORDS, rules)
    assert line != plain_reason("", rules)
    assert not re.search(r"\bR\d+\b", line)
    assert "—" not in line
    assert business_reasons([RULE_CUSTOMER_WORDS], rules) == [line]
    assert short_flag([RULE_CUSTOMER_WORDS], rules) == "Check the customer's message"


# what was read, and the sentence the merchant sees
ASKED_LINES = [
    (
        MATCH,
        "Customer asked for $10.00 on order ending RD-1. Reason: item didn't arrive.",
    ),
    (replace(MATCH, currency="INR", amount=Decimal("1500")), "1,500.00 INR"),
    (replace(MATCH, amount=None), "a refund but didn't say how much"),
    (replace(MATCH, order_id=None), "without naming an order"),
    (replace(MATCH, wants_other_destination=True), "sent somewhere else"),
]


@pytest.mark.edge
@pytest.mark.parametrize(("reading", "words"), ASKED_LINES)
def test_the_card_line_says_what_was_read(reading: CustomerReading, words: str) -> None:
    line = customer_asked(reading, read=True)
    assert words in line
    assert "—" not in line
    assert not re.search(r"\bR\d+\b", line)


def test_an_unread_message_is_said_plainly_and_no_message_says_nothing() -> None:
    assert customer_asked(None, read=False) == UNREAD_MESSAGE
    assert customer_asked(None, read=True) == ""


# the request's own order is already on screen; a different one must be named
ORDER_CASES = [("ORD-1", False), ("ORD-OTHER", True)]


@pytest.mark.edge
@pytest.mark.parametrize(("request_order", "named"), ORDER_CASES)
def test_the_order_is_named_only_when_it_differs(
    request_order: str, named: bool
) -> None:
    line = customer_asked(MATCH, read=True, order_id=request_order)
    assert ("order ending" in line) is named
