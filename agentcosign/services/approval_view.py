"""What the approver sees: facts from PayPal and one plain reason, in owner words.

The wording follows the product writing guide: the customer's name and the
amount lead, the reason is one sentence without rule codes or system words, and
facts come from PayPal at the moment the approver looks. The customer's name is
read live and shown, never stored.
"""

from collections.abc import Mapping, Sequence
from datetime import datetime, timedelta
from decimal import Decimal
from typing import Any

from agentcosign.core.config import PolicySettings
from agentcosign.models.policy import CustomerReading
from agentcosign.services.policy_engine import (
    ESCALATE_PRIORITY,
    RULE_EVIDENCE,
    RULE_SCREENING_DOWN,
    VELOCITY_WINDOW,
)

UNKNOWN_NAME = "the customer"


def plain_reason(rule_id: str, policy: PolicySettings) -> str:
    """One sentence saying why this request needs the owner."""
    auto = f"${policy.refund_auto_limit:,.2f}"
    velocity = policy.velocity_max_refunds_24h
    window = int(VELOCITY_WINDOW.total_seconds() // 3600)
    reasons = {
        "R2": f"Refunds over {auto} need your approval.",
        "R5": "This would take this customer past your limit of "
        f"{velocity} refunds in {window} hours.",
        "R6": "This order is in a dispute. If you refund it, you may still lose "
        "the dispute.",
        "R7": "Accepting a dispute gives up the money. This always needs your "
        "approval.",
        "R8": "These payout details are new or changed. Check them before you approve.",
        "R14": "This order changed after your assistant checked it. Review it again.",
        "R15": "We couldn't scan the customer's messages for misuse.",
        "R17": "Your assistant already refunded this order or this customer without "
        f"asking you. Refunds without asking stop at {auto} per order and per "
        f"customer in {window} hours.",
        "R18": "What the customer wrote doesn't match this refund, or gives a reason "
        "PayPal can't confirm, like a double charge or a missing earlier refund. "
        "Read their message first.",
    }
    return reasons.get(rule_id, "This one needs your approval.")


def refused_at_approval(rule_id: str, policy: PolicySettings) -> str:
    """Why an approved request could not go out after PayPal was checked again."""
    window = int(VELOCITY_WINDOW.total_seconds() // 3600)
    reasons = {
        "R4": "There isn't enough left on this order for this refund now.",
        "R5": "This customer reached your limit of "
        f"{policy.velocity_max_refunds_24h} refunds in {window} hours, so it is "
        "on hold.",
        "R10": "This refund would pass your daily refund limit of "
        f"${policy.daily_outflow_cap:,.2f}.",
        "R13": "The order's currency or account no longer matches.",
        "R14": "The order changed at PayPal after your assistant checked it.",
    }
    return reasons.get(rule_id, "It no longer fits your settings.")


SAFETY_PASSED = "passed"
SAFETY_UNAVAILABLE = "unavailable"
ONLY_SAFETY_REASON = (
    "This would normally be refunded on its own, but we couldn't scan the "
    "customer's messages for misuse."
)


def safety_check(triggered: Sequence[str]) -> str:
    """Whether the safety check ran. A check that did not run is never "passed"."""
    return SAFETY_UNAVAILABLE if RULE_SCREENING_DOWN in triggered else SAFETY_PASSED


def business_reasons(triggered: Sequence[str], policy: PolicySettings) -> list[str]:
    """Every business reason the request needs the owner, most serious first.

    The safety check is reported on its own line, so an outage never hides the
    reason a person would care about, such as a limit or repeat refunds.
    """
    order = [r for r in ESCALATE_PRIORITY if r != RULE_SCREENING_DOWN]
    order.append(RULE_EVIDENCE)
    lines = [plain_reason(rule, policy) for rule in order if rule in triggered]
    if not lines and RULE_SCREENING_DOWN in triggered:
        return [ONLY_SAFETY_REASON]
    return lines or [plain_reason("", policy)]


def scan_note(triggered: Sequence[str], policy: PolicySettings) -> str:
    """The scan line shown beside other reasons; empty when the scan ran or when
    the scan is already the only reason given."""
    business = [r for r in ESCALATE_PRIORITY if r != RULE_SCREENING_DOWN]
    others = any(rule in triggered for rule in [*business, RULE_EVIDENCE])
    if RULE_SCREENING_DOWN in triggered and others:
        return plain_reason(RULE_SCREENING_DOWN, policy)
    return ""


def refundable_now(facts: Mapping[str, str]) -> Decimal | None:
    """What PayPal says can still be refunded on the order, or None if unknown."""
    if not facts.get("paid"):
        return None
    return Decimal(facts["paid"]) - Decimal(facts["already_refunded"])


def left_after(facts: Mapping[str, str], amount: str) -> str:
    """What could still be refunded on the order after this refund, or blank."""
    now = refundable_now(facts)
    return "" if now is None else f"{now - Decimal(amount):.2f}"


def short_flag(triggered: Sequence[str], policy: PolicySettings) -> str:
    """A few words for a collapsed card: the most serious reason it needs you."""
    auto = f"${policy.refund_auto_limit:,.2f}"
    flags = {
        "R7": "Dispute claim",
        "R6": "Open dispute",
        "R8": "New payout details",
        "R14": "Order changed",
        "R17": "Already refunded without asking",
        "R18": "Check the customer's message",
        "R2": f"Over your {auto} limit",
    }
    for rule in ("R7", "R6", "R18", "R8", "R14", "R17", "R2"):
        if rule in triggered:
            return flags[rule]
    return "Safety check didn't run" if RULE_SCREENING_DOWN in triggered else ""


REASON_WORDS = {
    "not_received": "the item didn't arrive",
    "damaged": "the item arrived damaged",
    "not_as_described": "the item isn't as described",
    "wrong_item": "the wrong item was sent",
    "duplicate_charge": "they were charged twice",
    "refund_not_received": "an earlier refund didn't arrive",
    "subscription_cancelled": "they were billed after cancelling",
    "unrecognised_charge": "they don't recognise the charge",
    "changed_mind": "they changed their mind",
    "other": "another reason",
}
UNREAD_MESSAGE = "We couldn't read the customer's message. Read it yourself first."


def customer_asked(reading: CustomerReading | None, read: bool) -> str:
    """What the customer's own message asked for, as a fixed sentence built from
    the AI's reading; empty when the request came with no message."""
    if not read:
        return UNREAD_MESSAGE
    if reading is None:
        return ""
    if reading.amount is None:
        amount = "a refund but didn't say how much"
    elif reading.currency in (None, "USD"):
        amount = f"${reading.amount:,.2f} back"
    else:
        amount = f"{reading.amount:,.2f} {reading.currency} back"
    if reading.order_id:
        order = f" on order ending {order_ending(reading.order_id)}"
    else:
        order = " without naming an order"
    reason = REASON_WORDS.get(reading.reason or "other", REASON_WORDS["other"])
    line = f"Asked for {amount}{order}. Reason: {reason}."
    if reading.wants_other_destination:
        line += " They want the money sent somewhere else."
    return line


def money_view(
    facts: Mapping[str, str], amount: str, waiting: Decimal = Decimal("0")
) -> dict[str, str]:
    """The refundable amounts the approver sees, and whether this one still fits.

    The order can change while a request waits (for example a refund made in
    PayPal directly). The server re-checks before sending either way; this only
    stops the screen from offering a refund larger than what is left.
    """
    paypal_now = refundable_now(facts)
    if paypal_now is None:
        return {
            "refundable_now": "",
            "left_after": "",
            "fits": "unknown",
            "waiting_on_order": "",
        }
    # other refunds already waiting on this order are money promised too
    now = paypal_now - waiting
    fits = "yes" if Decimal(amount) <= now else "no"
    return {
        "refundable_now": f"{now:.2f}",
        "left_after": f"{now - Decimal(amount):.2f}",
        "fits": fits,
        "waiting_on_order": f"{waiting:.2f}",
    }


FAILURE_DETAILS = {
    "paypal_failed_later": "It didn't go through at PayPal. No money moved",
    "paypal_failed": "It didn't go through at PayPal. No money moved",
    "paypal_refused": "PayPal refused it. No money moved",
    "exceeds_refundable": "It was more than was left on the order",
    "evidence_changed": "The order changed, so it was stopped. No money moved",
}


def outcome_title(state: str) -> str:
    """How a decision reads in the request's history, or empty if it has none.

    The on-hold title names the rule's own window, so it can never disagree
    with the rule.
    """
    window = int(VELOCITY_WINDOW.total_seconds() // 3600)
    titles = {
        "pending_approval": "Sent to you for approval",
        "paused": f"On hold: this customer reached the {window}-hour refund limit",
        "executing": "Allowed by your settings",
        "denied": "Not refunded: outside your settings",
    }
    return titles.get(state, "")


def order_ending(order_id: str | None) -> str:
    """The last four characters of an order id, enough to tell orders apart."""
    return (order_id or "")[-4:]


def _decided_events(body: dict[str, Any]) -> list[dict[str, str]]:
    amount = f"${Decimal(str(body['amount'])):,.2f}"
    ending = order_ending(body.get("order_id"))
    events = [
        {
            "step": "asked",
            "title": "Your AI assistant asked",
            "detail": f"{amount} refund, order ending {ending}",
        }
    ]
    title = outcome_title(str(body.get("state")))
    if title:
        events.append({"step": "routed", "title": title, "detail": ""})
    return events


def display_name(name: str) -> str:
    """A name as people read it: all capitals or all lower case becomes title
    case; anything else is kept as typed, so names like McKay stay right."""
    if name.isupper() or name.islower():
        return " ".join(part.capitalize() for part in name.split())
    return name


def _recheck_event(body: dict[str, Any]) -> dict[str, str]:
    """An approval that a fresh check with PayPal stopped before any money moved."""
    approver = display_name(str(body.get("approver") or ""))
    if body.get("state") == "pending_approval":
        return {
            "step": "changed",
            "title": f"{approver} approved, then the order changed"
            if approver
            else "The order changed",
            "detail": "Sent back to you to review again. No money moved",
        }
    held = body.get("state") == "paused"
    return {
        "step": "refused_on_recheck",
        "title": f"{approver} approved, then PayPal was checked again",
        "detail": "It went on hold. No money moved"
        if held
        else "It didn't fit any more, so it was not refunded. No money moved",
    }


def _changed_event(body: dict[str, Any]) -> dict[str, str]:
    to = body.get("to")
    approver = display_name(str(body.get("approver") or ""))
    if approver and to == "executing":
        return {
            "step": "approved",
            "title": f"{approver} approved",
            "detail": "Confirmed with fingerprint or screen lock",
        }
    if approver and to == "denied":
        return {
            "step": "declined",
            "title": f"{approver} declined",
            "detail": "No money moved",
        }
    if to == "executed":
        done = body.get("paypal_status") == "COMPLETED"
        title = "Refunded by PayPal" if done else "Processing at PayPal"
        return {
            "step": "paypal_completed" if done else "paypal_accepted",
            "title": title,
            "detail": f"Reference {body.get('paypal_id', '')}",
        }
    if to == "failed":
        reason = str(body.get("reason", ""))
        return {
            "step": "failed",
            "title": "Not refunded",
            "detail": FAILURE_DETAILS.get(reason, "No money moved"),
        }
    if to == "expired":
        return {
            "step": "expired",
            "title": "Time ran out",
            "detail": "No money moved",
        }
    if to == "pending_approval":
        return {
            "step": "changed",
            "title": "The order changed",
            "detail": "Sent back to you to review again",
        }
    return {"step": "closed", "title": "Closed", "detail": "No money moved"}


def history_events(rows: Sequence[dict[str, Any]]) -> list[dict[str, str]]:
    """The audit trail for one request in owner words, oldest first.

    Only what the record shows: it never claims the customer was told anything,
    because the assistant's messages are not part of this record.
    """
    events: list[dict[str, str]] = []
    for row in rows:
        at = str(row["at"])
        kind = row["event"]
        found: list[dict[str, str]] = []
        if kind == "decided":
            found = _decided_events(row)
        elif kind == "state_changed":
            found = [_changed_event(row)]
        elif kind == "rechecked":
            found = [_recheck_event(row)]
        elif kind == "execution_attempt":
            found = [
                {
                    "step": "rechecked",
                    "title": "Checked with PayPal again",
                    "detail": "Right before sending",
                }
            ]
        elif kind == "released":
            title = outcome_title(str(row.get("state"))) or "Decided again"
            found = [
                {
                    "step": "released",
                    "title": "Checked again after the pause",
                    "detail": title,
                }
            ]
        elif kind == "paypal_status" and row.get("paypal_status") == "COMPLETED":
            found = [
                {
                    "step": "paypal_completed",
                    "title": "Refunded by PayPal",
                    "detail": "",
                }
            ]
        events.extend({"at": at, **item} for item in found)
    return events


def decide_by(created_at: str, open_seconds: int) -> str:
    """The moment the request closes (ISO time), after which nothing is sent."""
    return (
        datetime.fromisoformat(created_at) + timedelta(seconds=open_seconds)
    ).isoformat()


def _name_from(order: Mapping[str, Any]) -> tuple[str, str]:
    """Customer name and how they paid, from PayPal's order record."""
    source = order.get("payment_source") or {}
    card = source.get("card") or {}
    if card:
        last = str(card.get("last_digits", ""))
        return str(
            card.get("name") or UNKNOWN_NAME
        ), f"card ending {last}" if last else "card"
    wallet = source.get("paypal") or {}
    name = wallet.get("name") or (order.get("payer") or {}).get("name") or {}
    full = " ".join(str(name.get(k, "")) for k in ("given_name", "surname")).strip()
    return full or UNKNOWN_NAME, "PayPal"


def _money_total(entries: Any, statuses: frozenset[str]) -> Decimal:
    total = Decimal("0")
    for entry in entries or []:
        if isinstance(entry, Mapping) and entry.get("status") in statuses:
            total += Decimal(str(entry.get("amount", {}).get("value", "0")))
    return total


def order_facts(order: Mapping[str, Any]) -> dict[str, str]:
    """Name, payment method, amount paid and already refunded, as display text."""
    name, method = _name_from(order)
    units = order.get("purchase_units") or [{}]
    payments = units[0].get("payments") or {}
    paid = _money_total(
        payments.get("captures"),
        frozenset({"COMPLETED", "PARTIALLY_REFUNDED", "REFUNDED"}),
    )
    refunded = _money_total(
        payments.get("refunds"), frozenset({"COMPLETED", "PENDING"})
    )
    return {
        "customer": name,
        "paid_with": method,
        "paid": f"{paid:.2f}",
        "already_refunded": f"{refunded:.2f}",
    }


def unavailable_facts() -> dict[str, str]:
    """Shown when PayPal could not be reached; the decision still fails safe."""
    return {
        "customer": UNKNOWN_NAME,
        "paid_with": "",
        "paid": "",
        "already_refunded": "",
    }


SECTIONS = ("needs_decision", "cant_send", "paused", "sending", "failed")
_SECTION_OF_STATUS = {"paused": "paused", "sending": "sending", "failed_late": "failed"}
RECENT_DONE = 3


def placement(
    view: Mapping[str, Any], now: datetime, *, stopped: bool = False
) -> dict[str, str]:
    """Where a request belongs on the merchant's home and whether it can be sent.

    Only a waiting request with PayPal's facts in hand, an amount that still
    fits and time left can be sent; everything else is shown but never sendable.
    """
    status = str(view.get("status", ""))
    if status != "waiting":
        return {
            "section": _SECTION_OF_STATUS.get(status, "cant_send"),
            "can_send": "no",
            "cant_send_reason": "",
        }
    reason = ""
    if stopped:
        reason = "stopped"
    elif not view.get("paid"):
        reason = "paypal_unavailable"
    elif view.get("fits") != "yes":
        reason = "not_enough_left"
    elif now >= datetime.fromisoformat(str(view["decide_by"])):
        reason = "time_up"
    if reason:
        return {"section": "cant_send", "can_send": "no", "cant_send_reason": reason}
    return {"section": "needs_decision", "can_send": "yes", "cant_send_reason": ""}


def waiting_note(view: Mapping[str, Any]) -> str:
    """Says when other open requests hold money on the same order."""
    held = Decimal(str(view.get("waiting_on_order") or "0"))
    return f"${held:,.2f} waiting on another request" if held > 0 else ""


def read_mark(facts: Mapping[str, str], at: datetime | None) -> dict[str, str]:
    """Records whether this card's facts really came from PayPal, and when.

    Only a successful read carries a check time, so no card can claim to be up
    to date with PayPal when its read failed.
    """
    if at is not None and facts.get("paid"):
        return {"paypal_read": "yes", "checked_at": at.isoformat()}
    return {"paypal_read": "no", "checked_at": ""}


def paypal_checked(views: Sequence[Mapping[str, Any]]) -> str:
    """Whether every card that needed PayPal's facts got them.

    Returns yes, partial or no, and none when no card needed a read, so an
    empty home or one with only refunds already at PayPal claims no check.
    """
    tried = [v for v in views if v.get("paypal_read") in ("yes", "no")]
    if not tried:
        return "none"
    read = sum(1 for v in tried if v["paypal_read"] == "yes")
    if read == len(tried):
        return "yes"
    return "partial" if read else "no"


def checked_time(views: Sequence[Mapping[str, Any]], checked: str) -> str:
    """The oldest successful read, given only when every card that needed one got it."""
    if checked != "yes":
        return ""
    return min(str(v["checked_at"]) for v in views if v.get("paypal_read") == "yes")
