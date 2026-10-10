"""Headless order capture and refund behaviour on the PayPal SANDBOX.

Answers:
  * Can we create *and capture* an order from a script (card payment_source)?
  * Does create_refund work; does `PayPal-Request-Id` make refunds idempotent;
    where do refunds show up on the order and capture; does PayPal itself reject an
    over-refund (a second line of defence behind the gateway's own cumulative cap)?

Safe by construction: sandbox base URL is hard-coded, amounts are tiny, only IDs and
status codes are printed (never secrets). Evidence JSON is written to spikes/out/
(gitignored) and can later be sanitised into the committed evidence folder.

Run:  python spikes/s1_s2_order_refund.py
"""

import json
import sys
import time
import uuid
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import httpx

from agentcosign.core.config import get_settings

BASE = "https://api-m.sandbox.paypal.com"  # sandbox only; never configurable
OUT_DIR = Path(__file__).parent / "out"

# Candidate sandbox test cards (PayPal's card generator is the authoritative source;
# these are tried in order until one is accepted).
CANDIDATE_CARDS = [
    ("Visa", "4111111111111111"),
    ("Visa", "4012000077777777"),
    ("Mastercard", "5555555555554444"),
    ("Visa", "4032039317984658"),
]
BILLING = {
    "address_line_1": "123 Main St",
    "admin_area_2": "San Jose",
    "admin_area_1": "CA",
    "postal_code": "95131",
    "country_code": "US",
}
evidence: dict[str, Any] = {"started": datetime.now(UTC).isoformat(), "steps": []}


def step(name: str, **data: Any) -> None:
    evidence["steps"].append({"step": name, **data})
    brief = {k: v for k, v in data.items() if k != "body"}
    print(f"[{name}] {json.dumps(brief, default=str)}")


def issues(resp: httpx.Response) -> list[str]:
    try:
        body = resp.json()
    except ValueError:
        return [resp.text[:120]]
    found = [body.get("name", "")] + [
        d.get("issue", "") for d in body.get("details", [])
    ]
    return [i for i in found if i]


def get_token(client: httpx.Client) -> str:
    settings = get_settings()
    secret = settings.paypal_client_secret.get_secret_value()
    if not settings.paypal_client_id or not secret:
        sys.exit("PAYPAL_CLIENT_ID / PAYPAL_CLIENT_SECRET missing in .env")
    r = client.post(
        f"{BASE}/v1/oauth2/token",
        auth=(settings.paypal_client_id, secret),
        data={"grant_type": "client_credentials"},
        headers={"Accept": "application/json"},
    )
    r.raise_for_status()
    data = r.json()
    scopes = sorted(s.rsplit("/", 1)[-1] for s in data.get("scope", "").split())
    step(
        "token", status=r.status_code, expires_in=data.get("expires_in"), scopes=scopes
    )
    return str(data["access_token"])


def hdrs(token: str, request_id: str | None = None) -> dict[str, str]:
    h = {
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json",
        "Prefer": "return=representation",
    }
    if request_id:
        h["PayPal-Request-Id"] = request_id
    return h


def s1_create_and_capture(client: httpx.Client, token: str) -> dict[str, Any] | None:
    for brand, number in CANDIDATE_CARDS:
        body = {
            "intent": "CAPTURE",
            "purchase_units": [
                {
                    "reference_id": "agentcosign-s1",
                    "amount": {"currency_code": "USD", "value": "10.00"},
                }
            ],
            "payment_source": {
                "card": {
                    "number": number,
                    "expiry": "2030-12",
                    "security_code": "123",
                    "name": "Test Buyer",
                    "billing_address": BILLING,
                }
            },
        }
        t0 = time.perf_counter()
        r = client.post(
            f"{BASE}/v2/checkout/orders",
            headers=hdrs(token, str(uuid.uuid4())),
            json=body,
        )
        ms = round((time.perf_counter() - t0) * 1000)
        if r.status_code in (200, 201):
            order = r.json()
            pu = order["purchase_units"][0]
            cap = (pu.get("payments", {}).get("captures") or [{}])[0]
            step(
                "S1 create+capture OK",
                card=brand,
                http=r.status_code,
                ms=ms,
                order_id=order.get("id"),
                order_status=order.get("status"),
                capture_id=cap.get("id"),
                capture_status=cap.get("status"),
                payments_keys=sorted(pu.get("payments", {}).keys()),
            )
            return order
        step(
            "S1 card rejected", card=brand, http=r.status_code, ms=ms, issues=issues(r)
        )
    return None


def s2_refunds(client: httpx.Client, token: str, order: dict[str, Any]) -> None:
    pu = order["purchase_units"][0]
    capture_id = pu["payments"]["captures"][0]["id"]
    order_id = order["id"]
    url = f"{BASE}/v2/payments/captures/{capture_id}/refund"
    amount = {"currency_code": "USD", "value": "4.00"}

    # 1) First refund with a fixed request id.
    rid = str(uuid.uuid4())
    t0 = time.perf_counter()
    r1 = client.post(url, headers=hdrs(token, rid), json={"amount": amount})
    ms = round((time.perf_counter() - t0) * 1000)
    j1 = r1.json() if r1.content else {}
    step(
        "S2 refund #1",
        http=r1.status_code,
        ms=ms,
        refund_id=j1.get("id"),
        status=j1.get("status"),
        issues=issues(r1) if r1.status_code >= 400 else [],
    )

    # 2) Replay with the SAME request id: idempotent? (same id, no second refund)
    r1b = client.post(url, headers=hdrs(token, rid), json={"amount": amount})
    j1b = r1b.json() if r1b.content else {}
    step(
        "S2 replay same request-id",
        http=r1b.status_code,
        refund_id=j1b.get("id"),
        same_refund_id=(j1b.get("id") == j1.get("id")),
        issues=issues(r1b) if r1b.status_code >= 400 else [],
    )

    # 3) Second, different refund of 4.00 (cumulative 8.00 of 10.00).
    r2 = client.post(
        url, headers=hdrs(token, str(uuid.uuid4())), json={"amount": amount}
    )
    j2 = r2.json() if r2.content else {}
    step(
        "S2 refund #2 (new id)",
        http=r2.status_code,
        refund_id=j2.get("id"),
        status=j2.get("status"),
        issues=issues(r2) if r2.status_code >= 400 else [],
    )

    # 4) Over-refund: 5.00 would exceed the 10.00 capture -> PayPal should reject.
    over = {"amount": {"currency_code": "USD", "value": "5.00"}}
    r3 = client.post(url, headers=hdrs(token, str(uuid.uuid4())), json=over)
    step("S2 over-refund (cumulative 13 > 10)", http=r3.status_code, issues=issues(r3))

    # 5) Read-backs: where do refunds appear?
    ro = client.get(f"{BASE}/v2/checkout/orders/{order_id}", headers=hdrs(token))
    jo = ro.json()
    pay = jo["purchase_units"][0].get("payments", {})
    cap = (pay.get("captures") or [{}])[0]
    step(
        "S2 GET order",
        http=ro.status_code,
        order_status=jo.get("status"),
        payments_keys=sorted(pay.keys()),
        capture_status=cap.get("status"),
        refunds_on_order=len(pay.get("refunds", []))
        if "refunds" in pay
        else "no refunds key",
        capture_keys=sorted(cap.keys()),
    )
    rc = client.get(f"{BASE}/v2/payments/captures/{capture_id}", headers=hdrs(token))
    jc = rc.json()
    step(
        "S2 GET capture",
        http=rc.status_code,
        status=jc.get("status"),
        link_rels=sorted(link.get("rel", "") for link in jc.get("links", [])),
    )
    if j1.get("id"):
        rr = client.get(f"{BASE}/v2/payments/refunds/{j1['id']}", headers=hdrs(token))
        jr = rr.json()
        step(
            "S2 GET refund",
            http=rr.status_code,
            status=jr.get("status"),
            amount=jr.get("amount"),
        )


def main() -> None:
    OUT_DIR.mkdir(exist_ok=True)
    with httpx.Client(timeout=30) as client:
        token = get_token(client)
        order = s1_create_and_capture(client, token)
        if order is None:
            step(
                "S1 RESULT",
                passed=False,
                note="no candidate card accepted: need a card from the dashboard "
                "card generator, or ACDC is not enabled for this account",
            )
        else:
            step("S1 RESULT", passed=True)
            s2_refunds(client, token, order)
            step("S2 RESULT", note="see steps above")
    out = OUT_DIR / f"s1_s2_{datetime.now(UTC):%Y%m%dT%H%M%SZ}.json"
    out.write_text(json.dumps(evidence, indent=2, default=str), encoding="utf-8")
    print(f"\nevidence written to {out}")


if __name__ == "__main__":
    main()
