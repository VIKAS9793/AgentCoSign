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
from agentcosign.services.policy_engine import (
    ESCALATE_PRIORITY,
    RULE_EVIDENCE,
    RULE_SCREENING_DOWN,
)

UNKNOWN_NAME = "the customer"


def plain_reason(rule_id: str, policy: PolicySettings) -> str:
    """One sentence saying why this request needs the owner."""
    auto = f"${policy.refund_auto_limit:,.2f}"
    velocity = policy.velocity_max_refunds_24h
    reasons = {
        "R2": f"Refunds over {auto} need your OK.",
        "R5": f"This customer has reached your limit of {velocity} refunds "
        "in 24 hours.",
        "R6": "This order is in a dispute. If you send this refund, you may still "
        "lose the dispute.",
        "R7": "Accepting a dispute gives up the money. This always needs your OK.",
        "R8": "These payout details are new or changed. Check them before sending.",
        "R14": "This order changed after your assistant checked it. Please look again.",
        "R15": "We could not run our safety check. Please look at this one yourself.",
    }
    return reasons.get(rule_id, "This one needs your OK.")


SAFETY_PASSED = "passed"
SAFETY_UNAVAILABLE = "unavailable"
ONLY_SAFETY_REASON = (
    "Your assistant could usually send this on its own. It needs your OK because "
    "our safety check did not run."
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


def refundable_now(facts: Mapping[str, str]) -> Decimal | None:
    """What PayPal says can still be refunded on the order, or None if unknown."""
    if not facts.get("paid"):
        return None
    return Decimal(facts["paid"]) - Decimal(facts["already_refunded"])


def left_after(facts: Mapping[str, str], amount: str) -> str:
    """What could still be refunded on the order after this refund, or blank."""
    now = refundable_now(facts)
    return "" if now is None else f"{now - Decimal(amount):.2f}"


def money_view(facts: Mapping[str, str], amount: str) -> dict[str, str]:
    """The refundable amounts the approver sees, and whether this one still fits.

    The order can change while a request waits (for example a refund made in
    PayPal directly). The server re-checks before sending either way; this only
    stops the screen from offering a refund larger than what is left.
    """
    now = refundable_now(facts)
    if now is None:
        return {"refundable_now": "", "left_after": "", "fits": "unknown"}
    fits = "yes" if Decimal(amount) <= now else "no"
    return {
        "refundable_now": f"{now:.2f}",
        "left_after": left_after(facts, amount),
        "fits": fits,
    }


def decide_by(created_at: str, ttl_seconds: int) -> str:
    """The moment the request closes (ISO time), after which nothing is sent."""
    return (
        datetime.fromisoformat(created_at) + timedelta(seconds=ttl_seconds)
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
