"""Refund in PayPal directly, outside the gateway, to test what happens to a
request that is waiting (a merchant refunding from the PayPal dashboard).

No stamp is sent, exactly like a refund made by hand. Sandbox only.

Run:  python spikes/s11_outside_refund.py CAPTURE_ID AMOUNT
"""

import sys
import uuid

import httpx
from s1_s2_order_refund import BASE, get_token, hdrs


def main() -> None:
    capture_id, amount = sys.argv[1], sys.argv[2]
    body = {"amount": {"currency_code": "USD", "value": amount}}
    with httpx.Client(timeout=30) as client:
        token = get_token(client)
        response = client.post(
            f"{BASE}/v2/payments/captures/{capture_id}/refund",
            headers=hdrs(token, f"outside-{uuid.uuid4().hex[:10]}"),
            json=body,
        )
    refund = response.json()
    print(
        "outside refund:", response.status_code, refund.get("id"), refund.get("status")
    )


if __name__ == "__main__":
    main()
