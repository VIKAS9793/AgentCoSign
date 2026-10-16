"""PayPal sandbox client: the only code that talks to PayPal.

Two implementations share one interface. ``LivePayPalClient`` calls the sandbox
REST API. ``RecordedPayPalClient`` replays recorded responses so the hosted demo
and the test suite run with no credentials and no network.

Every write sends ``PayPal-Request-Id`` set to the gateway's request id, so a
retried call returns the original result instead of moving money twice. Ids that
reach a URL path are checked against PayPal's id format first, because they
arrive from the agent and must never be able to alter the path.
"""

import hashlib
import json
import re
import time
from collections.abc import Callable, Mapping
from decimal import Decimal
from pathlib import Path
from typing import Any, Protocol

import httpx
from pydantic import SecretStr

from agentcosign.core.exceptions import PayPalClientError
from agentcosign.models.policy import REQUEST_ID_PATTERN

SANDBOX_BASE = "https://api-m.sandbox.paypal.com"  # sandbox only; not configurable
REQUEST_ID_HEADER = "PayPal-Request-Id"
TIMEOUT_SECONDS = 15
VERIFIED = "SUCCESS"
NOT_VERIFIED = "FAILURE"
TOKEN_REFRESH_MARGIN_MS = 300_000
PAYPAL_ID = re.compile(r"^[A-Z0-9-]{1,64}$")
RETRYABLE_STATUSES = frozenset({429, 500, 502, 503, 504})

JsonObject = dict[str, Any]


def _monotonic_ms() -> int:
    return time.monotonic_ns() // 1_000_000


def checked_id(value: str | None, kind: str) -> str:
    """Return a PayPal id that is safe to place in a URL path, or refuse it."""
    if value is None or not PAYPAL_ID.match(value):
        raise PayPalClientError(f"invalid PayPal {kind} id", {"kind": kind})
    return value


def checked_request_id(value: str) -> str:
    """Return a request id that PayPal accepts as its idempotency header."""
    if not REQUEST_ID_PATTERN.fullmatch(value or ""):
        raise PayPalClientError("invalid request id", {"retryable": False})
    return value


class PayPalClient(Protocol):
    """The four calls the gateway makes."""

    async def get_order(self, order_id: str) -> JsonObject:
        """Read an order with its captures and refunds."""
        ...

    async def list_disputes(self, capture_id: str) -> list[JsonObject]:
        """List disputes raised against a capture."""
        ...

    async def refund_capture(
        self, capture_id: str, amount: Decimal, currency: str, request_id: str
    ) -> JsonObject:
        """Refund part or all of a capture, idempotently."""
        ...

    async def accept_claim(self, dispute_id: str, request_id: str) -> JsonObject:
        """Accept a dispute claim, conceding the disputed amount."""
        ...

    async def verify_webhook(
        self, headers: Mapping[str, str], event: JsonObject, webhook_id: str
    ) -> bool:
        """True only when PayPal says the webhook's signature verifies."""
        ...


def _error_from(response: httpx.Response) -> PayPalClientError:
    """Summarise a PayPal error without keeping the full response body."""
    try:
        body = response.json()
    except ValueError:
        body = {}
    issues = [
        d.get("issue", "") for d in body.get("details", []) if isinstance(d, dict)
    ]
    return PayPalClientError(
        f"PayPal returned {response.status_code}",
        {
            "status": response.status_code,
            "name": body.get("name", ""),
            "issues": [i for i in issues if i][:5],
            "retryable": response.status_code in RETRYABLE_STATUSES,
        },
    )


class LivePayPalClient:
    """Sandbox REST client with a cached token and one retry on expiry."""

    def __init__(
        self,
        client_id: str,
        client_secret: SecretStr,
        *,
        http: httpx.AsyncClient | None = None,
        clock: Callable[[], int] = _monotonic_ms,
    ) -> None:
        if not client_id or not client_secret.get_secret_value():
            raise PayPalClientError("PayPal sandbox credentials are not configured")
        self._client_id = client_id
        self._client_secret = client_secret
        self._http = http or httpx.AsyncClient(
            base_url=SANDBOX_BASE, timeout=TIMEOUT_SECONDS
        )
        self._clock = clock
        self._token: str | None = None
        self._token_expires_ms = 0

    async def aclose(self) -> None:
        """Release the connection pool."""
        await self._http.aclose()

    async def _fetch_token(self) -> str:
        response = await self._http.post(
            "/v1/oauth2/token",
            auth=(self._client_id, self._client_secret.get_secret_value()),
            data={"grant_type": "client_credentials"},
            headers={"Accept": "application/json"},
            timeout=TIMEOUT_SECONDS,
        )
        if response.status_code != 200:
            raise _error_from(response)
        body = response.json()
        lifetime_ms = int(body.get("expires_in", 0)) * 1000
        self._token = str(body["access_token"])
        self._token_expires_ms = self._clock() + lifetime_ms - TOKEN_REFRESH_MARGIN_MS
        return self._token

    async def _bearer(self) -> str:
        if self._token is None or self._clock() >= self._token_expires_ms:
            return await self._fetch_token()
        return self._token

    async def _send(
        self,
        method: str,
        path: str,
        *,
        body: JsonObject | None = None,
        params: Mapping[str, str] | None = None,
        request_id: str | None = None,
    ) -> Any:
        for attempt in range(2):
            headers = {
                "Authorization": f"Bearer {await self._bearer()}",
                "Content-Type": "application/json",
                "Prefer": "return=representation",
            }
            if request_id:
                headers[REQUEST_ID_HEADER] = request_id
            try:
                response = await self._http.request(
                    method,
                    path,
                    json=body,
                    params=params,
                    headers=headers,
                    timeout=TIMEOUT_SECONDS,
                )
            except httpx.HTTPError as exc:
                raise PayPalClientError(
                    "PayPal could not be reached",
                    {"error": type(exc).__name__, "retryable": True},
                ) from exc
            if response.status_code == 401 and attempt == 0:
                self._token = None  # expired early: fetch a new token once
                continue
            if response.status_code >= 400:
                raise _error_from(response)
            return response.json() if response.content else {}
        raise PayPalClientError("PayPal rejected the credentials", {"status": 401})

    async def get_order(self, order_id: str) -> JsonObject:
        """Read an order with its captures and refunds."""
        order = checked_id(order_id, "order")
        result: JsonObject = await self._send("GET", f"/v2/checkout/orders/{order}")
        return result

    async def list_disputes(self, capture_id: str) -> list[JsonObject]:
        """List disputes raised against a capture."""
        capture = checked_id(capture_id, "capture")
        body = await self._send(
            "GET",
            "/v1/customer/disputes",
            params={"disputed_transaction_id": capture},
        )
        items = body.get("items", []) if isinstance(body, dict) else []
        return [item for item in items if isinstance(item, dict)]

    async def refund_capture(
        self, capture_id: str, amount: Decimal, currency: str, request_id: str
    ) -> JsonObject:
        """Refund part or all of a capture, idempotently."""
        capture = checked_id(capture_id, "capture")
        request_id = checked_request_id(request_id)
        result: JsonObject = await self._send(
            "POST",
            f"/v2/payments/captures/{capture}/refund",
            body=_refund_body(amount, currency, request_id),
            request_id=request_id,
        )
        return result

    async def accept_claim(self, dispute_id: str, request_id: str) -> JsonObject:
        """Accept a dispute claim, conceding the disputed amount."""
        dispute = checked_id(dispute_id, "dispute")
        request_id = checked_request_id(request_id)
        result: JsonObject = await self._send(
            "POST",
            f"/v1/customer/disputes/{dispute}/accept-claim",
            body={"note": "Accepted after review by the merchant."},
            request_id=request_id,
        )
        return result

    async def verify_webhook(
        self, headers: Mapping[str, str], event: JsonObject, webhook_id: str
    ) -> bool:
        """Ask PayPal whether a webhook's signature verifies.

        Only ``verification_status == SUCCESS`` in the body counts; an HTTP 200
        alone does not.
        """
        body = {
            "auth_algo": headers["paypal-auth-algo"],
            "cert_url": headers["paypal-cert-url"],
            "transmission_id": headers["paypal-transmission-id"],
            "transmission_sig": headers["paypal-transmission-sig"],
            "transmission_time": headers["paypal-transmission-time"],
            "webhook_id": webhook_id,
            "webhook_event": event,
        }
        result = await self._send(
            "POST", "/v1/notifications/verify-webhook-signature", body=body
        )
        return isinstance(result, dict) and (
            result.get("verification_status") == VERIFIED
        )


# Shown in the customer's PayPal history and emails with the refund.
REFUND_NOTE_TO_PAYER = (
    "Your refund is on its way to your original payment method. "
    "Thank you for your patience."
)


def _refund_body(amount: Decimal, currency: str, request_id: str) -> JsonObject:
    """Refund request stamped with our request id so we can recognise it later."""
    return {
        "amount": {"value": str(amount), "currency_code": currency},
        "custom_id": request_id,
        "invoice_id": request_id,
        "note_to_payer": REFUND_NOTE_TO_PAYER,
    }


class RecordedPayPalClient:
    """Replays recorded PayPal responses; writes are simulated in memory.

    Refunds and claim acceptances return a deterministic id derived from the
    request id, so replaying a request returns the same result, and refunds are
    added to the order so later reads see them, as PayPal does.
    """

    def __init__(
        self,
        orders: Mapping[str, JsonObject],
        disputes: Mapping[str, list[JsonObject]] | None = None,
        refund_status: str = "COMPLETED",
    ) -> None:
        self._refund_status = refund_status
        self._orders = {
            key: json.loads(json.dumps(value)) for key, value in orders.items()
        }
        self._disputes = {key: list(value) for key, value in (disputes or {}).items()}
        self._writes: dict[str, JsonObject] = {}
        # a simulator must never vouch for a real event: tests opt in explicitly
        self._webhook_status: str | None = NOT_VERIFIED

    @classmethod
    def from_directory(cls, root: Path) -> "RecordedPayPalClient":
        """Load ``orders/<id>.json`` and ``disputes/<capture id>.json`` files."""
        orders = {
            p.stem: json.loads(p.read_text("utf-8")) for p in root.glob("orders/*.json")
        }
        disputes = {
            p.stem: json.loads(p.read_text("utf-8"))
            for p in root.glob("disputes/*.json")
        }
        return cls(orders, disputes)

    def set_webhook_verification(self, status: str | None) -> None:
        """What PayPal answers to webhook checks; None means PayPal is unreachable."""
        self._webhook_status = status

    async def verify_webhook(
        self, headers: Mapping[str, str], event: JsonObject, webhook_id: str
    ) -> bool:
        """Answer as PayPal would: verified, not verified, or unreachable."""
        if self._webhook_status is None:
            raise PayPalClientError("PayPal could not be reached", {"retryable": True})
        return self._webhook_status == VERIFIED

    async def get_order(self, order_id: str) -> JsonObject:
        """Return the recorded order, including refunds made since."""
        order = self._orders.get(checked_id(order_id, "order"))
        if order is None:
            raise PayPalClientError(
                "order not found", {"status": 404, "retryable": False}
            )
        copy: JsonObject = json.loads(json.dumps(order))
        return copy

    async def list_disputes(self, capture_id: str) -> list[JsonObject]:
        """Return recorded disputes for a capture."""
        return list(self._disputes.get(checked_id(capture_id, "capture"), []))

    async def refund_capture(
        self, capture_id: str, amount: Decimal, currency: str, request_id: str
    ) -> JsonObject:
        """Simulate an idempotent refund against a recorded capture."""
        capture = checked_id(capture_id, "capture")
        request_id = checked_request_id(request_id)
        if request_id in self._writes:
            return dict(self._writes[request_id])
        purchase = self._purchase_with_capture(capture)
        refund = {
            **_refund_body(amount, currency, request_id),
            "id": "RF-" + _short_id(request_id),
            "status": self._refund_status,
        }
        purchase.setdefault("payments", {}).setdefault("refunds", []).append(refund)
        self._writes[request_id] = refund
        return dict(refund)

    async def accept_claim(self, dispute_id: str, request_id: str) -> JsonObject:
        """Simulate accepting a recorded dispute claim."""
        checked_id(dispute_id, "dispute")
        request_id = checked_request_id(request_id)
        result = self._writes.setdefault(
            request_id, {"id": dispute_id, "status": "RESOLVED"}
        )
        return dict(result)

    def set_refund_status(self, request_id: str, status: str) -> None:
        """Simulate PayPal changing a refund's status later (for example FAILED)."""
        refund = self._writes[request_id]
        refund["status"] = status
        for order in self._orders.values():
            for unit in order.get("purchase_units", []):
                for item in unit.get("payments", {}).get("refunds", []):
                    if item.get("custom_id") == request_id:
                        item["status"] = status

    def _purchase_with_capture(self, capture_id: str) -> JsonObject:
        for order in self._orders.values():
            for unit in order.get("purchase_units", []):
                captures = unit.get("payments", {}).get("captures", [])
                if any(c.get("id") == capture_id for c in captures):
                    unit_object: JsonObject = unit
                    return unit_object
        raise PayPalClientError(
            "capture not found", {"status": 404, "retryable": False}
        )


def _short_id(request_id: str) -> str:
    return hashlib.sha256(request_id.encode("utf-8")).hexdigest()[:12].upper()
