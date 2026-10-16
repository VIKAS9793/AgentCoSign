"""ASGI middleware that applies traffic protection before any route runs.

Order of defence for every HTTP request: body size, then the per-client buckets,
penalty box and in-flight cap in the TrafficGuard. Rejections are small, honest
JSON responses with a Retry-After header, never an unhandled error.
"""

import hmac
import json
import time
from collections.abc import Callable
from dataclasses import dataclass
from http.cookies import CookieError, SimpleCookie

from starlette.types import ASGIApp, Message, Receive, Scope, Send

from agentcosign.core.config import LimitSettings
from agentcosign.core.limits import Admission, Tier, TrafficGuard, resolve_client_key

REVIEWER_COOKIE = "acs_reviewer"
REVIEWER_HEADER = "x-reviewer-token"
STATUS_TOO_LARGE = 413

_MESSAGES = {
    "rate_limited": "Too many requests from your network. Please slow down.",
    "blocked": "Requests from your network are paused for a short while.",
    "busy": "The demo is busy right now. Please try again in a moment.",
    "too_large": "That request is larger than this demo accepts.",
}


@dataclass(frozen=True)
class ClientContext:
    """Who is calling, as resolved by the protection layer."""

    key: str
    tier: Tier


def monotonic_ms() -> int:
    """Monotonic clock in whole milliseconds (immune to wall-clock changes)."""
    return time.monotonic_ns() // 1_000_000


def token_matches(candidate: str | None, secret: str) -> bool:
    """Constant-time check of a presented reviewer token; off when none is set."""
    if not secret or not candidate:
        return False
    return hmac.compare_digest(candidate.encode(), secret.encode())


class _BodyTooLarge(Exception):
    """Raised while streaming a request body that exceeds the size cap."""


def _limit_body(receive: Receive, max_bytes: int) -> Receive:
    seen = 0

    async def limited() -> Message:
        """Pass messages through, aborting once the body exceeds the cap."""
        nonlocal seen
        message = await receive()
        if message["type"] == "http.request":
            seen += len(message.get("body", b""))
            if seen > max_bytes:
                raise _BodyTooLarge
        return message

    return limited


def _headers(scope: Scope) -> dict[str, str]:
    out: dict[str, str] = {}
    for raw_name, raw_value in scope.get("headers", []):
        out.setdefault(raw_name.decode("latin-1").lower(), raw_value.decode("latin-1"))
    return out


def _cookie_value(cookie_header: str | None, name: str) -> str | None:
    if not cookie_header:
        return None
    jar: SimpleCookie = SimpleCookie()
    try:
        jar.load(cookie_header)
    except CookieError:
        return None
    morsel = jar.get(name)
    return morsel.value if morsel else None


async def _reject(send: Send, status: int, reason: str, retry_after: int) -> None:
    body = json.dumps(
        {
            "error": reason,
            "message": _MESSAGES.get(reason, "Request not accepted."),
            "retry_after_seconds": retry_after,
        }
    ).encode()
    headers = [
        (b"content-type", b"application/json"),
        (b"content-length", str(len(body)).encode()),
        (b"cache-control", b"no-store"),
    ]
    if retry_after:
        headers.append((b"retry-after", str(retry_after).encode()))
    await send({"type": "http.response.start", "status": status, "headers": headers})
    await send({"type": "http.response.body", "body": body})


class ProtectionMiddleware:
    """Admit, limit and account for every HTTP request."""

    def __init__(
        self,
        app: ASGIApp,
        *,
        guard: TrafficGuard,
        limits: LimitSettings,
        reviewer_token: str,
        clock: Callable[[], int] = monotonic_ms,
    ) -> None:
        self.app = app
        self._guard = guard
        self._limits = limits
        self._reviewer_token = reviewer_token
        self._clock = clock

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        """Handle one ASGI connection."""
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        headers = _headers(scope)
        peer = (scope.get("client") or (None, 0))[0]
        edge_header = self._limits.client_ip_header
        key = resolve_client_key(
            peer,
            headers.get("x-forwarded-for"),
            self._limits.trusted_proxy_hops,
            headers.get(edge_header) if edge_header else None,
        )
        presented = headers.get(REVIEWER_HEADER) or _cookie_value(
            headers.get("cookie"), REVIEWER_COOKIE
        )
        tier = (
            Tier.REVIEWER
            if token_matches(presented, self._reviewer_token)
            else Tier.PUBLIC
        )
        declared = headers.get("content-length", "")
        if declared.isdigit() and int(declared) > self._limits.max_body_bytes:
            await _reject(send, STATUS_TOO_LARGE, "too_large", 0)
            return
        admission = self._guard.admit(key, tier, scope["path"], self._clock())
        if not admission.allowed:
            await self._refuse(send, admission)
            return
        scope.setdefault("state", {})["client"] = ClientContext(key=key, tier=tier)
        await self._run(scope, receive, send, admission)

    async def _refuse(self, send: Send, admission: Admission) -> None:
        await _reject(
            send, admission.status, admission.reason, admission.retry_after_seconds
        )

    async def _run(
        self, scope: Scope, receive: Receive, send: Send, admission: Admission
    ) -> None:
        started = False

        async def tracking_send(message: Message) -> None:
            """Forward the message and remember whether a response has begun."""
            nonlocal started
            if message["type"] == "http.response.start":
                started = True
            await send(message)

        try:
            await self.app(
                scope,
                _limit_body(receive, self._limits.max_body_bytes),
                tracking_send,
            )
        except _BodyTooLarge:
            if not started:
                await _reject(send, STATUS_TOO_LARGE, "too_large", 0)
        finally:
            if admission.counted:
                self._guard.release(admission)
