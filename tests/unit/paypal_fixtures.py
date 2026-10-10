"""Synthetic PayPal responses shaped like the sandbox's (not recordings).

The field names and nesting follow the order, capture, refund and dispute
responses seen in the live sandbox checks; values are invented for tests.
"""

from typing import Any

ACCOUNT = "ACCT-1"


def order_json(
    order: str = "ORD-1",
    capture: str = "CAP-1",
    captured: str = "100.00",
    refunds: tuple[tuple[str, str], ...] = (),
    merchant: str | None = ACCOUNT,
    card: bool = True,
    currency: str = "USD",
) -> dict[str, Any]:
    """An order with one purchase unit and one capture."""
    payments: dict[str, Any] = {
        "captures": [
            {
                "id": capture,
                "status": "COMPLETED",
                "amount": {"value": captured, "currency_code": currency},
            }
        ],
        "refunds": [
            {
                "id": f"RF-{index}",
                "status": status,
                "amount": {"value": value, "currency_code": currency},
            }
            for index, (value, status) in enumerate(refunds)
        ],
    }
    unit: dict[str, Any] = {"payments": payments}
    if merchant is not None:
        unit["payee"] = {"merchant_id": merchant}
    body: dict[str, Any] = {
        "id": order,
        "status": "COMPLETED",
        "purchase_units": [unit],
    }
    if card:
        body["payment_source"] = {
            "card": {"brand": "VISA", "last_digits": "4242", "name": "Jane Buyer"}
        }
    return body


def dispute_json(
    dispute: str = "DSP-1", status: str = "OPEN", amount: str = "40.00"
) -> dict[str, Any]:
    """A dispute record as listed for a capture."""
    return {
        "dispute_id": dispute,
        "status": status,
        "dispute_amount": {"value": amount, "currency_code": "USD"},
    }
