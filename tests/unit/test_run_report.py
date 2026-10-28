"""The closing step of a run, and the safe-state line on failed steps.

Edge cases (written before the code):

| Case | Expected |
|---|---|
| No refund proposed | Nothing sent |
| No refund proposed, but the checks caught a trick | The trick is named; nothing sent |
| Refused by the rules | Nothing sent; an attack found in the message is named |
| Waiting for approval, or on hold | Nothing sent yet |
| Declined by the merchant | Nothing sent |
| PayPal confirmed the refund | Sent, with the amount |
| PayPal answered but not confirmed | Not called sent; money moved unknown |
| No answer from PayPal yet | Not called sent nor "nothing sent": unknown |
| Approved, then a fresh check stopped it | Nothing sent |
| PayPal failed the refund | Not completed, nothing moved |
| Failed PayPal read | Reading only, nothing sent |
| PayPal refused a write | No money moved by this call |
| PayPal's answer lost on a write | Unknown; read again before any retry |
| A model gives no answer | Models never move money; fallback, then the rules |
| A timed step fails | Reported with where things stand, then raised |
"""

from decimal import Decimal

import pytest

from agentcosign.clients.llm import model_safe_state
from agentcosign.clients.paypal import _report_call, safe_state
from agentcosign.core import trace
from agentcosign.services import run_report
from agentcosign.services.run_report import NOTHING, UNKNOWN, Ending

TEN = Decimal("10.00")
HOW = run_report.AUTOMATICALLY

# (ending, words the headline must contain, status mark, money moved)
NOTHING_MOVED_CASES = [
    (Ending(None), "No refund proposed", trace.WARN),
    (Ending(None, caught=True), "Attack in the customer's message caught", trace.OK),
    (Ending("deny", "denied", ("R14",)), "Refused by the rules", trace.OK),
    (Ending("deny", "denied", ("R11", "R14")), "Attack", trace.OK),
    (Ending("escalate", "pending_approval"), "Waiting for the merchant", trace.OK),
    (Ending("pause", "paused"), "On hold", trace.OK),
    (Ending("rejected", "denied"), "Declined by the merchant", trace.OK),
    (Ending("approved", "denied", amount=TEN), "fresh check", trace.OK),
    (Ending("approved", "failed", amount=TEN), "did not complete", trace.FAIL),
]
UNCONFIRMED_CASES = [
    (
        Ending("allow", "executing", paypal_status="PENDING", amount=TEN),
        "not confirmed",
    ),
    (Ending("allow", "executing", amount=TEN), "waiting for PayPal"),
]


@pytest.mark.edge
@pytest.mark.parametrize(("ending", "words", "mark"), NOTHING_MOVED_CASES)
def test_nothing_sent_is_said_only_when_nothing_moved(
    ending: Ending, words: str, mark: str
) -> None:
    title, status, moved = run_report.headline(ending, HOW)
    assert words in title
    assert status == mark
    assert moved == NOTHING
    assert " sent automatically" not in title


@pytest.mark.edge
@pytest.mark.parametrize(("ending", "words"), UNCONFIRMED_CASES)
def test_a_refund_paypal_has_not_confirmed_is_never_called_sent(
    ending: Ending, words: str
) -> None:
    title, status, moved = run_report.headline(ending, HOW)
    assert words in title
    assert status == trace.WARN
    assert moved == UNKNOWN
    assert "nothing sent" not in title.lower()


def test_a_confirmed_refund_is_sent_with_its_amount() -> None:
    ending = Ending("allow", "executed", paypal_status="COMPLETED", amount=TEN)
    title, status, moved = run_report.headline(ending, HOW)
    assert title == "Refund of $10.00 sent automatically: PayPal confirmed"
    assert (status, moved) == (trace.OK, "$10.00")


@pytest.mark.edge
def test_a_failed_paypal_read_sent_nothing() -> None:
    assert safe_state("GET", None) == "Reading only: nothing was sent"
    assert safe_state("GET", 500).startswith("Reading only")


@pytest.mark.edge
def test_a_refused_write_moved_no_money_but_a_lost_answer_is_unknown() -> None:
    assert safe_state("POST", 422) == "PayPal refused it: no money moved by this call"
    for lost in (None, 500, 503):
        line = safe_state("POST", lost)
        assert line.startswith("Answer lost, so treated as unknown")
        assert "no money moved" not in line


@pytest.mark.edge
def test_a_model_with_no_answer_never_moves_money() -> None:
    assert model_safe_state(primary=True).endswith("the fallback model is tried next")
    assert model_safe_state(primary=False).endswith("the rules decide without it")


@pytest.mark.edge
def test_a_failed_timed_step_says_where_things_stand() -> None:
    record = trace.Trace()
    token = trace.use(record)
    try:
        with (
            pytest.raises(ValueError),
            trace.timed("rules", "x", on_fail="Nothing sent"),
        ):
            raise ValueError
    finally:
        trace._current.reset(token)
    (step,) = record.after(0)
    assert step.status == trace.FAIL
    assert step.detail["safe"] == "Nothing sent"


@pytest.mark.edge
def test_a_paypal_step_carries_its_returned_id_but_never_the_body() -> None:
    record = trace.Trace()
    token = trace.use(record)
    answer = {"id": "RF-1", "status": "COMPLETED", "payer": {"email": "a@b.c"}}
    try:
        _report_call("POST", "/v2/payments/captures/C/refund", 0.0, 201, "rq", answer)
    finally:
        trace._current.reset(token)
    (step,) = record.after(0)
    assert step.detail["paypal_id"] == "RF-1"
    assert step.detail["paypal_status"] == "COMPLETED"
    assert "a@b.c" not in str(step.detail)
