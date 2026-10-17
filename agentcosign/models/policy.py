"""Inputs to the policy engine.

Everything the engine decides on is a frozen value object, so a decision is a
pure function of its inputs and can be replayed exactly from the audit record.
Trust differs by type:

* ``Proposal``: claims made by the agent. Untrusted.
* ``OrderEvidence``: facts read from PayPal by the gateway. Trusted.
* ``LedgerContext``: counters computed from the gateway's own ledger. Trusted.
* ``Signals``: outputs of screening and extraction models. Advisory inputs that
  can only make a decision stricter, never authorise anything.
"""

import re
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from enum import StrEnum

# The request id is sent to PayPal as its idempotency header, which accepts at
# most 38 single-byte characters. The character set also keeps an id supplied
# by the agent from breaking an HTTP header.
REQUEST_ID_PATTERN = re.compile(r"^[A-Za-z0-9._:-]{1,38}$")


class ScreeningVerdict(StrEnum):
    """Result of the prompt-injection screening layer."""

    CLEAN = "clean"
    INJECTION = "injection"
    UNAVAILABLE = "unavailable"


@dataclass(frozen=True, slots=True)
class Proposal:
    """What the agent asks to do. Every field is a claim, not a fact."""

    request_id: str
    tool: str
    amount: Decimal
    currency: str
    order_id: str | None = None
    dispute_id: str | None = None
    payee_id: str | None = None
    reference: str | None = None
    claimed_captured: Decimal | None = None
    claimed_refunded: Decimal | None = None


@dataclass(frozen=True, slots=True)
class OrderEvidence:
    """Facts about an order as read from PayPal, never supplied by the agent."""

    order_id: str
    currency: str
    captured_amount: Decimal
    refunded_amount: Decimal
    account_id: str
    has_open_dispute: bool
    fetched_at: datetime
    dispute_id: str | None = None
    dispute_amount: Decimal | None = None
    # Keyed digest identifying the paying customer across orders (card brand,
    # last four and cardholder name for card payments). Never the raw values.
    customer_key: str | None = None
    # The capture a refund is issued against.
    capture_id: str | None = None
    # Refunds on the order that carry our stamp: (our request id, PayPal refund
    # id, PayPal status). Lets us recognise our own refund exactly.
    tagged_refunds: tuple[tuple[str, str, str], ...] = ()


@dataclass(frozen=True, slots=True)
class LedgerContext:
    """Counters from the gateway's own ledger, reservations included."""

    expected_account_id: str
    reserved_for_order: Decimal = Decimal("0")
    refunds_24h_for_customer: int = 0
    # When each of those refunds was counted, so a pause can say exactly when
    # the customer is back under the limit.
    customer_refund_times: tuple[datetime, ...] = ()
    outflow_24h: Decimal = Decimal("0")
    # Refunds sent (or being sent) without a person deciding: on this order ever,
    # and for this customer in the last 24 hours.
    auto_for_order: Decimal = Decimal("0")
    auto_24h_for_customer: Decimal = Decimal("0")
    known_payee: bool = False
    duplicate_payout_in_window: bool = False
    # the merchant pressed stop: nothing moves until they turn refunds back on
    refunds_stopped: bool = False


@dataclass(frozen=True, slots=True)
class Signals:
    """Screening and extraction outputs. They can only tighten a decision."""

    screening: ScreeningVerdict = ScreeningVerdict.CLEAN
    prescreen_hit: bool = False
    extracted_amount: Decimal | None = None
