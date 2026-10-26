"""The closing step of a run in the live step record: one headline saying what
happened to the money, and what the run took to get there.

Built only from what the server knows: the decision, the request's state,
PayPal's own status and the steps already reported. A refund is called sent
only when PayPal confirmed it; when PayPal has not answered, the headline says
so instead of guessing either way.
"""

import time
from dataclasses import dataclass
from decimal import Decimal

from agentcosign.core import trace
from agentcosign.models.domain import Decision
from agentcosign.services.policy_engine import RULE_INJECTION

AUTOMATICALLY = "automatically"
AFTER_APPROVAL = "after the merchant's fingerprint or PIN"
NOTHING = "$0.00"
UNKNOWN = "unknown until PayPal answers"
_CONFIRMED = "COMPLETED"


@dataclass(frozen=True, slots=True)
class Begun:
    """Where a run started in the step record, and when."""

    seq: int
    # guardrail: allow float-annotation -- monotonic seconds, not money
    started: float


@dataclass(frozen=True, slots=True)
class Ending:
    """What the server knows at the end of a run."""

    decision: str | None  # None when no refund was proposed
    state: str | None = None
    triggered: tuple[str, ...] = ()
    paypal_status: str | None = None
    amount: Decimal | None = None
    currency: str = "USD"


def begin(layer: str, title: str, expects: str = "") -> Begun:
    """Report the first step of a run and remember where the run starts."""
    begun = Begun(trace.position(), time.monotonic())
    trace.emit(layer, title, expects=expects)
    return begun


def silent_begin() -> Begun:
    """Remember where a run starts, for a run whose first step reports itself."""
    return Begun(trace.position(), time.monotonic())


def headline(ending: Ending, how: str) -> tuple[str, str, str]:
    """The headline, its status mark and the money that moved."""
    money = f"${ending.amount}" if ending.amount is not None else "the refund"
    match ending.decision:
        case None:
            return "No refund proposed: nothing sent", trace.WARN, NOTHING
        case "rejected":
            return "Declined by the merchant: nothing sent", trace.OK, NOTHING
        case Decision.DENY if RULE_INJECTION in ending.triggered:
            title = "Attack in the customer's message caught: refused, nothing sent"
            return title, trace.OK, NOTHING
        case Decision.DENY:
            return "Refused by the rules: nothing sent", trace.OK, NOTHING
        case Decision.ESCALATE:
            title = "Waiting for the merchant's approval: nothing sent yet"
            return title, trace.OK, NOTHING
        case Decision.PAUSE:
            title = "On hold, too many refunds for this customer: nothing sent yet"
            return title, trace.OK, NOTHING
    return _sending(ending, money, how)


def _sending(ending: Ending, money: str, how: str) -> tuple[str, str, str]:
    """A refund the rules or the merchant let through: only PayPal says sent."""
    if ending.paypal_status == _CONFIRMED:
        sent = f"${ending.amount}" if ending.amount is not None else UNKNOWN
        return f"Refund of {money} sent {how}: PayPal confirmed", trace.OK, sent
    if ending.paypal_status is not None:
        title = f"Refund of {money} is with PayPal: not confirmed yet"
        return title, trace.WARN, UNKNOWN
    match ending.state:
        case "denied" | "paused" | "pending_approval":
            title = "Not sent: a fresh check before sending stopped it"
            return title, trace.OK, NOTHING
        case "failed":
            return "PayPal did not complete the refund", trace.FAIL, NOTHING
    return "Sending: waiting for PayPal's answer", trace.WARN, UNKNOWN


def finish(begun: Begun, ending: Ending, how: str) -> None:
    """Report the closing step: the headline and what the run took."""
    title, status, moved = headline(ending, how)
    steps = trace.since(begun.seq)
    layers = [step.layer for step in steps]
    trace.emit(
        "outcome",
        title,
        status,
        int((time.monotonic() - begun.started) * 1000),
        money_moved=moved,
        triggered=", ".join(ending.triggered),
        steps=len(steps),
        paypal_calls=layers.count("paypal"),
        ai_steps=layers.count("ai"),
        record_entries=layers.count("ledger"),
    )
