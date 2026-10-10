"""What the approver sees: facts from PayPal and one plain reason, in owner words.

The wording follows the product writing guide: the customer's name and the
amount lead, the reason is one sentence without rule codes or system words, and
facts come from PayPal at the moment the approver looks. The customer's name is
read live and shown, never stored.
"""

from collections.abc import Mapping
from datetime import datetime, timedelta
from decimal import Decimal
from typing import Any

from agentcosign.core.config import PolicySettings

UNKNOWN_NAME = "the customer"


def plain_reason(rule_id: str, policy: PolicySettings) -> str:
    """One sentence saying why this request needs the owner."""
    auto = f"${policy.refund_auto_limit:,.2f}"
    velocity = policy.velocity_max_refunds_24h
    reasons = {
        "R2": f"Refunds over {auto} need your OK.",
        "R5": f"This customer has had more than {velocity} refunds "
        "in the last 24 hours.",
        "R6": "This order is in a dispute. If you send this refund, you may still "
        "lose the dispute.",
        "R7": "Accepting a dispute gives up the money. This always needs your OK.",
        "R8": "These payout details are new or changed. Check them before sending.",
        "R14": "This order changed after your assistant checked it. Please look again.",
        "R15": "We could not run our safety check. Please look at this one yourself.",
    }
    return reasons.get(rule_id, "This one needs your OK.")


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
