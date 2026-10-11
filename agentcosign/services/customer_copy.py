"""What the merchant's assistant tells the customer, word for word.

The gateway supplies these lines so the assistant never improvises a reason. They
never accuse, never mention limits or rules, never reveal a threshold, and promise
a time rather than an outcome, because a re-check can still ask the merchant or
decline. Refunds always go back to the original payment method, so no line offers
another destination.
"""

import math
from datetime import datetime
from decimal import Decimal

SECONDS_PER_HOUR = 3600


def first_name(full_name: str, unknown: str) -> str | None:
    """The customer's first name for a greeting, or None when PayPal had none."""
    name = full_name.strip()
    if not name or name == unknown:
        return None
    return name.split()[0]


def _greeting(name: str | None) -> str:
    return f"Thanks, {name}." if name else "Thanks."


def _money(amount: str) -> str:
    return f"${Decimal(amount):,.2f}"


def hours_until(moment: datetime, now: datetime) -> int:
    """Whole hours until a moment, rounded up and at least one."""
    seconds = (moment - now).total_seconds()
    return max(1, math.ceil(seconds / SECONDS_PER_HOUR))


def _within(hours: int) -> str:
    return "within 1 hour" if hours == 1 else f"within {hours} hours"


def paused(name: str | None, order: str, amount: str, hours: int) -> str:
    """First answer when a refund request is held for a later check."""
    return (
        f"{_greeting(name)} We've received your refund request for order {order} "
        f"({_money(amount)}). We look at some requests a little later to keep every "
        "account safe, and yours is one of them. You don't need to do anything. "
        f"We'll update you {_within(hours)}."
    )


def still_paused(order: str, hours: int) -> str:
    """Answer when the customer asks again while the request is held."""
    return (
        f"Your request for order {order} is saved and on track. We'll update you "
        f"{_within(hours)}. You don't need to send it again."
    )


def with_team(order: str, hours: int) -> str:
    """The request now waits for the merchant's own decision."""
    return (
        f"Your request for order {order} is with our team now. We'll update you "
        f"{_within(hours)}."
    )


def sent(name: str | None, order: str, amount: str) -> str:
    """PayPal confirmed the refund."""
    opener = f"Good news, {name}:" if name else "Good news:"
    return (
        f"{opener} your {_money(amount)} refund for order {order} is on its way "
        "to your original payment method."
    )


def sending(order: str) -> str:
    """Approved, but PayPal has not confirmed yet."""
    return (
        f"Your refund for order {order} is being sent now. We'll confirm as soon as "
        "it's done. You don't need to do anything."
    )


def declined(order: str) -> str:
    """The merchant decided not to refund, or a re-check could not allow it."""
    return (
        f"We're sorry, we can't make a refund for order {order}. If you have "
        "questions, reply here and someone from our team will help."
    )


FORBIDDEN_WORDS = (
    "limit",
    "blocked",
    "denied",
    "rejected",
    "suspicious",
    "fraud",
    "abuse",
    "policy",
    "rule",
    "too many",
)
