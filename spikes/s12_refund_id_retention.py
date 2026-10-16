"""Measure how long PayPal remembers a refund's PayPal-Request-Id (sandbox only).

Our executor retries an unfinished refund with the same request id. That is only
safe while PayPal still recognises the id. This measures it instead of assuming.

Run from the repository root:
    python spikes/s12_refund_id_retention.py start     # refund $5 with a known id
    python spikes/s12_refund_id_retention.py replay    # same id, same body, later

The state file holds ids only, never credentials, in the ignored data/ folder.
"""

import json
import sys
import uuid
from datetime import UTC, datetime
from pathlib import Path

import httpx

sys.path.insert(0, str(Path(__file__).resolve().parent))
from make_order import create_captured_order  # noqa: E402
from s1_s2_order_refund import BASE, get_token, hdrs  # noqa: E402

STATE = Path("data") / "s12_retention.json"
REFUND_BODY = {"amount": {"value": "5.00", "currency_code": "USD"}}


def _refund(
    client: httpx.Client, token: str, capture_id: str, request_id: str
) -> httpx.Response:
    return client.post(
        f"{BASE}/v2/payments/captures/{capture_id}/refund",
        headers=hdrs(token, request_id),
        json=REFUND_BODY,
    )


def start() -> None:
    with httpx.Client(timeout=30) as client:
        token = get_token(client)
        order = create_captured_order(client, token, "20.00", "Retention Check")
        if order is None:
            sys.exit("No sandbox order could be created.")
        capture_id = order["purchase_units"][0]["payments"]["captures"][0]["id"]
        request_id = f"s12-{uuid.uuid4()}"
        first = _refund(client, token, capture_id, request_id)
        refund = first.json()
        STATE.parent.mkdir(exist_ok=True)
        STATE.write_text(
            json.dumps(
                {
                    "order_id": order["id"],
                    "capture_id": capture_id,
                    "request_id": request_id,
                    "refund_id": refund.get("id"),
                    "first_status": first.status_code,
                    "started_at": datetime.now(UTC).isoformat(),
                }
            ),
            encoding="utf-8",
        )
        print(f"order {order['id']} refund {refund.get('id')} HTTP {first.status_code}")


def replay() -> None:
    state = json.loads(STATE.read_text(encoding="utf-8"))
    hours = (
        datetime.now(UTC) - datetime.fromisoformat(state["started_at"])
    ).total_seconds() / 3600
    with httpx.Client(timeout=30) as client:
        token = get_token(client)
        again = _refund(client, token, state["capture_id"], state["request_id"])
        refund_id = again.json().get("id")
        same = refund_id == state["refund_id"]
        print(f"after {hours:.1f} h: HTTP {again.status_code}, refund {refund_id}")
        print(
            "RETAINED: same refund returned"
            if same
            else "NOT RETAINED: PayPal treated it as new"
        )


if __name__ == "__main__":
    {"start": start, "replay": replay}[sys.argv[1]]()
