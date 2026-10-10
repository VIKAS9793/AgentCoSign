"""Turn PayPal's order and dispute records into the evidence the policy trusts.

Only facts PayPal reports are used: the captured amount, refunds already made,
the merchant account, the currency, open disputes and the card used. Anything
missing, ambiguous or inconsistent raises, and the gateway treats that as "no
evidence", which the policy denies.

Refunds that are still pending count as refunded: money that may already be on
its way back must not be refunded a second time.
"""

from collections.abc import Mapping, Sequence
from datetime import datetime
from decimal import Decimal, InvalidOperation
from typing import Any

from agentcosign.clients.paypal import PayPalClient
from agentcosign.core.exceptions import PayPalClientError
from agentcosign.models.policy import OrderEvidence
from agentcosign.services.identity import card_fingerprint, payer_key

COUNTED_REFUND_STATUSES = frozenset({"COMPLETED", "PENDING"})
CLOSED_DISPUTE_STATUSES = frozenset({"RESOLVED"})


def _money(field: Any) -> tuple[Decimal, str]:
    if not isinstance(field, Mapping):
        raise PayPalClientError("PayPal amount is missing")
    try:
        value = Decimal(str(field["value"]))
        currency = str(field["currency_code"])
    except (KeyError, InvalidOperation) as exc:
        raise PayPalClientError("PayPal amount is not readable") from exc
    if not value.is_finite():
        raise PayPalClientError("PayPal amount is not a number")
    return value, currency


def _single(items: Any, what: str) -> Mapping[str, Any]:
    if (
        not isinstance(items, list)
        or len(items) != 1
        or not isinstance(items[0], Mapping)
    ):
        raise PayPalClientError(f"expected exactly one {what} on the order")
    item: Mapping[str, Any] = items[0]
    return item


def _refunded(payments: Mapping[str, Any], currency: str) -> Decimal:
    total = Decimal("0")
    for refund in payments.get("refunds", []) or []:
        if not isinstance(refund, Mapping):
            raise PayPalClientError("PayPal refund record is not readable")
        if refund.get("status") not in COUNTED_REFUND_STATUSES:
            continue
        value, refund_currency = _money(refund.get("amount"))
        if refund_currency != currency:
            raise PayPalClientError("refund currency differs from the capture")
        total += value
    return total


def _open_dispute(
    disputes: Sequence[Mapping[str, Any]],
) -> tuple[bool, str | None, Decimal | None]:
    """First dispute that is not closed.

    A record without an id still counts as open: missing data must never read
    as "no dispute".
    """
    for dispute in disputes:
        if dispute.get("status") in CLOSED_DISPUTE_STATUSES:
            continue
        dispute_id = dispute.get("dispute_id")
        amount = dispute.get("dispute_amount")
        value = _money(amount)[0] if amount is not None else None
        return True, (str(dispute_id) if dispute_id else None), value
    return False, None, None


def _customer_key(order: Mapping[str, Any], secret: bytes) -> str | None:
    """Card payments: card fingerprint. Wallet payments: PayPal's payer id."""
    card = (order.get("payment_source") or {}).get("card") or {}
    if not isinstance(card, Mapping) or not card:
        payer = (order.get("payer") or {}).get("payer_id")
        return payer_key(secret, str(payer)) if payer else None
    return card_fingerprint(
        secret,
        str(card.get("brand", "")),
        str(card.get("last_digits", "")),
        str(card.get("name", "")),
    )


def order_evidence_from(
    order: Mapping[str, Any],
    disputes: Sequence[Mapping[str, Any]],
    *,
    fetched_at: datetime,
    secret: bytes,
) -> OrderEvidence:
    """Map PayPal's order and dispute records to evidence, or raise."""
    unit = _single(order.get("purchase_units"), "purchase unit")
    payments = unit.get("payments") or {}
    capture = _single(payments.get("captures"), "capture")
    captured, currency = _money(capture.get("amount"))
    merchant = (unit.get("payee") or {}).get("merchant_id")
    if not order.get("id") or not capture.get("id") or not merchant:
        raise PayPalClientError("PayPal order is missing an id or merchant")
    disputed, dispute_id, dispute_amount = _open_dispute(disputes)
    return OrderEvidence(
        order_id=str(order["id"]),
        currency=currency,
        captured_amount=captured,
        refunded_amount=_refunded(payments, currency),
        account_id=str(merchant),
        has_open_dispute=disputed,
        fetched_at=fetched_at,
        dispute_id=dispute_id,
        dispute_amount=dispute_amount,
        customer_key=_customer_key(order, secret),
        capture_id=str(capture["id"]),
    )


async def fetch_evidence(
    client: PayPalClient, order_id: str, *, now: datetime, secret: bytes
) -> OrderEvidence:
    """Read the order and its disputes from PayPal and map them to evidence."""
    order = await client.get_order(order_id)
    unit = _single(order.get("purchase_units"), "purchase unit")
    capture = _single((unit.get("payments") or {}).get("captures"), "capture")
    disputes = await client.list_disputes(str(capture.get("id", "")))
    return order_evidence_from(order, disputes, fetched_at=now, secret=secret)
