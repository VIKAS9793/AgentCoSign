"""Create one captured card order in the PayPal sandbox, to have something to refund.

Prints the order id, the capture id and your sandbox merchant id (the value for
PAYPAL_MERCHANT_ID). Sandbox only, sandbox test cards only; no real money.

Run:  python spikes/make_order.py 50.00 ["Cardholder Name"]
"""

import sys
import uuid

import httpx
from s1_s2_order_refund import BASE, BILLING, CANDIDATE_CARDS, get_token, hdrs


def create_captured_order(
    client: httpx.Client, token: str, amount: str, name: str = "Test Buyer"
) -> dict | None:
    """Create and capture an order, trying the sandbox test cards in turn."""
    for _brand, number in CANDIDATE_CARDS:
        body = {
            "intent": "CAPTURE",
            "purchase_units": [
                {
                    "reference_id": "agentcosign-demo",
                    "amount": {"currency_code": "USD", "value": amount},
                }
            ],
            "payment_source": {
                "card": {
                    "number": number,
                    "expiry": "2030-12",
                    "security_code": "123",
                    "name": name,
                    "billing_address": BILLING,
                }
            },
        }
        response = client.post(
            f"{BASE}/v2/checkout/orders",
            headers=hdrs(token, str(uuid.uuid4())),
            json=body,
        )
        if response.status_code in (200, 201):
            return response.json()
    return None


def main() -> None:
    amount = sys.argv[1] if len(sys.argv) > 1 else "50.00"
    name = sys.argv[2] if len(sys.argv) > 2 else "Test Buyer"
    with httpx.Client(timeout=30) as client:
        token = get_token(client)
        order = create_captured_order(client, token, amount, name)
    if order is None:
        sys.exit("No sandbox test card was accepted.")
    unit = order["purchase_units"][0]
    capture = unit["payments"]["captures"][0]
    print(f"order_id     {order['id']}")
    value = capture["amount"]["value"]
    print(f"capture_id   {capture['id']}  ({capture['status']}, {value} USD)")
    print(f"merchant_id  {unit.get('payee', {}).get('merchant_id', '(not returned)')}")


if __name__ == "__main__":
    main()
