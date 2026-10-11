"""Does PayPal keep our own tag on a refund, and show it when we read the order?

Refunds 1.00 on an existing sandbox capture with custom_id, invoice_id and
note_to_payer set, then reads the refund and the order back and prints which of
those fields each read returns. Sandbox only; prints ids and field names, never
tokens or full payloads.

Run:  python spikes/s10_refund_tags.py ORDER_ID CAPTURE_ID
"""

import sys
import uuid

import httpx
from s1_s2_order_refund import BASE, get_token, hdrs

TAGGED = ("custom_id", "invoice_id", "note_to_payer")


def main() -> None:
    order_id, capture_id = sys.argv[1], sys.argv[2]
    tag = f"spike-tag-{uuid.uuid4().hex[:8]}"
    body = {
        "amount": {"currency_code": "USD", "value": "1.00"},
        "custom_id": tag,
        "invoice_id": tag,
        "note_to_payer": "Your refund is on its way. Thank you for your patience.",
    }
    with httpx.Client(timeout=30) as client:
        token = get_token(client)
        created = client.post(
            f"{BASE}/v2/payments/captures/{capture_id}/refund",
            headers={**hdrs(token, tag), "Prefer": "return=representation"},
            json=body,
        )
        print("refund create:", created.status_code)
        refund = created.json()
        print("  id:", refund.get("id"), "status:", refund.get("status"))
        print("  fields returned:", [f for f in TAGGED if f in refund])
        refund_url = f"{BASE}/v2/payments/refunds/{refund.get('id')}"
        fetched = client.get(refund_url, headers=hdrs(token)).json()
        print("refund read: status:", fetched.get("status"))
        kept = {f: fetched.get(f) == body[f] for f in TAGGED if f in fetched}
        print("  fields returned:", kept)
        order_url = f"{BASE}/v2/checkout/orders/{order_id}"
        order = client.get(order_url, headers=hdrs(token)).json()
    refunds = order["purchase_units"][0]["payments"].get("refunds", [])
    ours = [r for r in refunds if r.get("id") == refund.get("id")]
    print("order read: refunds listed:", len(refunds))
    if ours:
        print("  our refund keys:", sorted(ours[0].keys()))
        present = {f: ours[0].get(f) == body[f] for f in TAGGED if f in ours[0]}
        print("  tag present:", present)


if __name__ == "__main__":
    main()
