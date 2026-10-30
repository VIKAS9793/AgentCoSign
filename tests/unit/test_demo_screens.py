"""What the app shows after a test scenario, run in Node from the shipped script.

Edge cases (written before the code):

| Case | Expected |
|---|---|
| PayPal confirmed the refund | "refunded to" the customer, with PayPal's reference |
| Allowed, PayPal not answered yet | "Waiting for PayPal": not sent, not "no money" |
| Allowed, PayPal answered failed | No money moved, PayPal named as not completing |
| Allowed, stopped before sending | No money moved, and not blamed on PayPal |
| Refused by the checks | No money moved |
| No refund suggested | No money moved |
| No refund, the checks caught a trick | A likely trick, nothing moved, see activity |
| The scenario's own request changes | No notice repeats what its screen says |
| That request later fails | Still announced |
| Home update while a finger is down | Not drawn: the list never moves under a tap |
| Two updates during one touch | The newest is kept, notices compare with the first |
| Finger lifts and the tap opened a request | The held update is dropped |
| Finger lifts and home is still showing | The held update is drawn, once |
| Home update with no touch | Drawn at once |
| Any other request changes | Announced as before |
"""

import json
from typing import Any

import pytest

from tests.unit.web_js import run_js, script_function

ENDING = ["demoEnding"]
NOTICES = ["money", "noticesFor"]
ORDER = {"customer": "Riya Sharma", "amount": "9.00"}


def _ending(
    outcome: dict[str, Any] | None, caught: bool = False, recorded: bool = True
) -> Any:
    result = {**ORDER, "outcome": outcome, "caught": caught, "recorded": recorded}
    return run_js(ENDING, f"demoEnding({json.dumps(result)})")


def _allowed(**extra: Any) -> dict[str, Any]:
    return {"request_id": "r1", "decision": "allow", **extra}


# (outcome, first words of the headline, words that must not appear)
NOTHING_MOVED = [
    (_allowed(state="failed", execution="paypal_failed"), "PayPal didn't complete"),
    (_allowed(state="failed", execution="exceeds_refundable"), "Not sent: it would"),
    ({"request_id": "r1", "decision": "deny", "state": "denied"}, "Refused."),
    (None, "No refund suggested."),
]


def test_a_refund_paypal_confirmed_names_the_customer_and_reference() -> None:
    outcome = _allowed(state="executed", paypal_status="COMPLETED", paypal_id="RF-1")
    kind, title, _, reference = _ending(outcome)
    assert (kind, title, reference) == ("sent", "$9.00 refunded to Riya Sharma", "RF-1")


@pytest.mark.edge
def test_a_refund_paypal_has_not_answered_is_waiting_not_sent() -> None:
    kind, title, lines, reference = _ending(_allowed(state="executing"))
    assert (kind, title, reference) == ("check", "Waiting for PayPal", "")
    assert "No money moved" not in " ".join([title, *lines])


@pytest.mark.edge
@pytest.mark.parametrize(("outcome", "starts"), NOTHING_MOVED)
def test_nothing_moved_is_said_only_where_nothing_reached_the_customer(
    outcome: dict[str, Any] | None, starts: str
) -> None:
    kind, title, _, reference = _ending(outcome)
    assert title.startswith(starts)
    assert "No money moved" in title
    assert "refunded to" not in title
    assert (kind, reference) == ("stopped", "")


@pytest.mark.edge
def test_stopped_before_sending_is_not_blamed_on_paypal() -> None:
    _, title, _, _ = _ending(_allowed(state="failed", execution="exceeds_refundable"))
    assert "PayPal didn't" not in title


def _notices(shown: str, before: dict[str, Any], after: dict[str, Any]) -> Any:
    expression = (
        f"(shownByRun = {json.dumps(shown)}, "
        f"noticesFor({json.dumps(before)}, {json.dumps(after)}))"
    )
    return run_js(NOTICES, expression, constants=("shownByRun",))


def _home(**sections: list[dict[str, str]]) -> dict[str, Any]:
    names = ("needs_decision", "paused", "sending", "failed")
    return {
        "sections": {name: sections.get(name, []) for name in names},
        "done": {"recent": sections.get("recent", [])},
    }


SENDING = {"request_id": "r1", "amount": "9.00", "customer": "Riya Sharma"}
SENT = {**SENDING, "outcome": "executed", "paypal_status": "COMPLETED"}
OTHER = {"request_id": "r2", "amount": "40.00", "customer": "Arjun Mehta"}


@pytest.mark.edge
def test_the_scenarios_own_request_is_not_announced_again() -> None:
    sent = _notices("r1", _home(sending=[SENDING]), _home(recent=[SENT]))
    new = _notices("r2", _home(), _home(needs_decision=[OTHER]))
    assert sent == []
    assert new == []


@pytest.mark.edge
def test_a_later_failure_of_that_request_is_still_announced() -> None:
    notes = _notices("r1", _home(sending=[SENDING]), _home(failed=[SENDING]))
    assert notes == [
        "The $9.00 refund to Riya Sharma didn't go through. No money reached them."
    ]


def test_other_requests_are_announced_as_before() -> None:
    notes = _notices("r1", _home(), _home(needs_decision=[OTHER]))
    assert notes == ["New refund request: $40.00 for Arjun Mehta"]


@pytest.mark.edge
def test_a_run_marks_its_request_before_the_dashboard_reloads() -> None:
    run = script_function("runDemo")
    assert run.index("shownByRun = result.outcome") < run.index("openHome()")


@pytest.mark.edge
def test_a_caught_trick_says_so_and_that_nothing_moved() -> None:
    kind, title, lines, reference = _ending(None, caught=True)
    assert (kind, reference) == ("stopped", "")
    assert title == "Looked like a trick. No money moved."
    assert "refund activity" in " ".join(lines)
    _, clean_title, _, _ = _ending(None)
    assert clean_title.startswith("No refund suggested.")


@pytest.mark.edge
def test_a_caught_trick_that_was_not_recorded_does_not_point_to_activity() -> None:
    kind, title, lines, _ = _ending(None, caught=True, recorded=False)
    assert (kind, title) == ("stopped", "Looked like a trick. No money moved.")
    assert "refund activity" not in " ".join(lines)


HOME = ["showHomeUpdate", "releaseHomeUpdate"]
# stands in for the page: what was drawn and announced
PAGE = (
    "var touching = %s, heldHome = null, current = {view: 'home'};"
    "var drawn = [], told = [];"
    "function renderHome(data) { drawn.push(data); }"
    "function noticesFor(previous, next) { return [previous + '>' + next]; }"
    "function toast(note) { told.push(note); }"
)


def _page_after(touching: bool, steps: str) -> Any:
    """Run page steps, then report what the merchant saw."""
    flag = "true" if touching else "false"
    expression = (
        f"(function () {{ {PAGE % flag} {steps};"
        " return {drawn: drawn, told: told, held: heldHome !== null}; })()"
    )
    return run_js([], _with_functions(expression))


def _with_functions(expression: str) -> str:
    bodies = " ".join(script_function(name) + "}" for name in HOME)
    return expression.replace("(function () {", "(function () {" + bodies, 1)


@pytest.mark.edge
def test_a_home_update_with_no_touch_is_drawn_at_once() -> None:
    seen = _page_after(False, "showHomeUpdate('new', 'old')")
    assert seen == {"drawn": ["new"], "told": ["old>new"], "held": False}


@pytest.mark.edge
def test_a_home_update_never_moves_the_list_under_a_finger() -> None:
    seen = _page_after(True, "showHomeUpdate('new', 'old')")
    assert seen == {"drawn": [], "told": [], "held": True}


@pytest.mark.edge
def test_two_updates_in_one_touch_keep_the_newest_and_the_first_comparison() -> None:
    steps = (
        "showHomeUpdate('one', 'old'); showHomeUpdate('two', 'one');"
        "touching = false; releaseHomeUpdate()"
    )
    assert _page_after(True, steps) == {
        "drawn": ["two"],
        "told": ["old>two"],
        "held": False,
    }


@pytest.mark.edge
def test_a_tap_that_opened_a_request_drops_the_held_update() -> None:
    steps = (
        "showHomeUpdate('new', 'old'); touching = false;"
        "current.view = 'review'; releaseHomeUpdate()"
    )
    assert _page_after(True, steps) == {"drawn": [], "told": [], "held": False}


@pytest.mark.edge
def test_a_held_update_waits_for_a_second_finger_to_lift() -> None:
    steps = "showHomeUpdate('new', 'old'); releaseHomeUpdate()"
    assert _page_after(True, steps) == {"drawn": [], "told": [], "held": True}
