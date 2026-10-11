"""Every line the customer can hear is kind, accusation-free and promises a time."""

import re
from datetime import UTC, datetime, timedelta

import pytest

from agentcosign.clients.paypal import REFUND_NOTE_TO_PAYER
from agentcosign.services import customer_copy as copy

NOW = datetime(2026, 11, 2, 12, 0, tzinfo=UTC)
LINES = [
    copy.paused("Priya", "ORD-1", "40.00", 7),
    copy.paused(None, "ORD-1", "40.00", 1),
    copy.still_paused("ORD-1", 3),
    copy.with_team("ORD-1", 1),
    copy.sent("Priya", "ORD-1", "40.00"),
    copy.sent(None, "ORD-1", "40.00"),
    copy.sending("ORD-1"),
    copy.declined("ORD-1"),
    copy.failed_late("ORD-1"),
    REFUND_NOTE_TO_PAYER,
]


@pytest.mark.parametrize("line", LINES)
def test_no_customer_line_accuses_or_mentions_limits(line: str) -> None:
    lowered = line.lower()
    for word in copy.FORBIDDEN_WORDS:
        assert word not in lowered, (word, line)
    assert not re.search(r"\d+\s+refunds", line)  # the threshold is never revealed
    assert "—" not in line


def test_the_pause_line_promises_a_time_not_an_outcome() -> None:
    line = copy.paused("Priya", "ORD-1", "40.00", 7)
    assert "within 7 hours" in line
    assert "You don't need to do anything" in line
    assert "will be refunded" not in line and "approved" not in line


def test_hours_round_up_and_never_drop_below_one() -> None:
    assert copy.hours_until(NOW + timedelta(minutes=61), NOW) == 2
    assert copy.hours_until(NOW + timedelta(seconds=5), NOW) == 1
    assert copy.hours_until(NOW - timedelta(hours=1), NOW) == 1


def test_a_missing_name_is_never_guessed() -> None:
    assert copy.first_name("the customer", "the customer") is None
    assert copy.first_name("  ", "the customer") is None
    assert copy.first_name("Priya Nair", "the customer") == "Priya"
