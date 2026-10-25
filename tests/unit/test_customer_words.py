"""A refund goes out alone only when the customer's own words ask for exactly it.

Edge cases (written before the code):

| Case | Expected |
|---|---|
| Reading matches order, amount, currency; ordinary reason | Allowed |
| "10" asked, 10.00 proposed | Allowed (same amount) |
| Different order, no order, different amount, no amount | Merchant |
| Different currency; money asked to go elsewhere | Merchant |
| Charged twice, earlier refund missing, unrecognised charge, changed mind | Merchant |
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
]


def _decide(signals: Signals, **proposal: Any) -> tuple[Decision, tuple[str, ...]]:
    built, evidence, ledger, _, rules, screening = BASE.build()
    built = replace(built, **proposal) if proposal else built
    result = evaluate(built, evidence, ledger, signals, rules, screening, NOW)
    return result.decision, result.triggered_rules


def _read(reading: CustomerReading | None) -> Signals:
    return Signals(reading_status=ReadingStatus.READ, reading=reading)


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
        "Asked for $10.00 back on order ending RD-1. Reason: the item didn't arrive.",
    ),
    (replace(MATCH, currency="INR", amount=Decimal("1500")), "1,500.00 INR back"),
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
