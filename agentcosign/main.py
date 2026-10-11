"""AgentCoSign — Main Application Entrypoint.

FastAPI application factory for the policy gateway.
"""

import asyncio
import logging
import sqlite3
from collections.abc import AsyncGenerator, Awaitable, Callable
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

from fastapi import FastAPI, Request, Response

from agentcosign import __version__
from agentcosign.api.gateway_routes import router as gateway_router
from agentcosign.api.protection import ProtectionMiddleware, monotonic_ms
from agentcosign.api.site import register_site
from agentcosign.api.traffic import router as traffic_router
from agentcosign.clients.paypal import (
    LivePayPalClient,
    PayPalClient,
    RecordedPayPalClient,
)
from agentcosign.core.config import Settings, get_settings
from agentcosign.core.exceptions import AgentCoSignError
from agentcosign.core.limits import TrafficGuard
from agentcosign.services.gateway import Gateway
from agentcosign.services.ledger import Ledger
from agentcosign.services.passkeys import PasskeyRegistry

logger = logging.getLogger("agentcosign")

EXPIRY_INTERVAL_SECONDS = 30
RECORDINGS_DIR = (
    Path(__file__).resolve().parent.parent / "docs" / "evidence" / "recordings"
)

# The approver UI is same-origin and script-light, so a strict CSP is cheap.
# LLM-generated text is untrusted and must only ever be rendered as text.
_SECURITY_HEADERS = {
    "Content-Security-Policy": (
        "default-src 'self'; img-src 'self' data:; style-src 'self'; "
        "script-src 'self'; frame-ancestors 'none'; base-uri 'none'; "
        "form-action 'self'"
    ),
    "X-Content-Type-Options": "nosniff",
    "X-Frame-Options": "DENY",
    "Referrer-Policy": "no-referrer",
    "Permissions-Policy": "geolocation=(), camera=(), microphone=()",
}


async def _expire_forever(gateway: Gateway) -> None:
    """Expire approvals left waiting past the window, releasing reservations, and
    finish executions whose PayPal answer was lost."""
    while True:
        await asyncio.sleep(EXPIRY_INTERVAL_SECONDS)
        try:
            await asyncio.to_thread(gateway.expire)
        except (AgentCoSignError, sqlite3.Error):
            logger.exception("approval expiry failed; will retry")
        try:
            await gateway.release_due()
        except (AgentCoSignError, sqlite3.Error):
            logger.exception("releasing paused refunds failed; will retry")
        try:
            await gateway.settle_pending()
        except (AgentCoSignError, sqlite3.Error):
            logger.exception("re-reading pending refunds failed; will retry")
        try:
            await gateway.finish_unfinished()
        except (AgentCoSignError, sqlite3.Error):
            logger.exception("finishing unanswered executions failed; will retry")


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncGenerator[None, None]:
    """Log the active run mode and run approval expiry in the background."""
    settings: Settings = app.state.settings
    expiry = asyncio.create_task(_expire_forever(app.state.gateway))
    logger.info(
        "AgentCoSign v%s env=%s hermetic=%s replay_mode=%s paypal_execution=%s",
        __version__,
        settings.env,
        settings.is_hermetic,
        settings.replay_mode,
        settings.paypal_execution,
    )
    if not settings.paypal_merchant_id:
        logger.warning("PAYPAL_MERCHANT_ID is not set: every order will be refused")
    yield
    expiry.cancel()
    logger.info("AgentCoSign shutting down cleanly")


def _install_security_headers(app: FastAPI) -> None:
    """Add the strict response headers to every response (no CORS on purpose).

    The approver PWA is served same-origin and agents are server-side callers, so an
    explicit allowlist is added only if a real need appears.
    """

    @app.middleware("http")
    async def add_security_headers(
        request: Request, call_next: Callable[[Request], Awaitable[Response]]
    ) -> Response:
        """Set CSP and friends; mark API responses as non-cacheable."""
        response = await call_next(request)
        for header, value in _SECURITY_HEADERS.items():
            response.headers.setdefault(header, value)
        if request.url.path.startswith("/api"):
            response.headers["Cache-Control"] = "no-store"
        return response


def _install_traffic_protection(app: FastAPI, settings: Settings) -> TrafficGuard:
    """Attach the guard, the protection middleware and its routes.

    Added before the security-header middleware so that refusals also carry the
    security headers.
    """
    guard = TrafficGuard(settings.limits)
    app.state.guard = guard
    app.state.settings = settings
    app.add_middleware(
        ProtectionMiddleware,
        guard=guard,
        limits=settings.limits,
        reviewer_token=settings.reviewer_token.get_secret_value(),
    )
    app.include_router(traffic_router)
    return guard


def _register_system_routes(
    app: FastAPI, settings: Settings, guard: TrafficGuard
) -> None:
    """Register the liveness endpoint (also reports the active run modes)."""

    @app.api_route("/health", methods=["GET", "HEAD"], tags=["System"])
    async def health_check() -> dict[str, Any]:
        """Liveness, run-mode and capacity report for monitors and the demo banner.

        HEAD is answered too, because uptime monitors commonly probe with it.
        """
        return {
            "status": "healthy",
            "service": "AgentCoSign",
            "version": __version__,
            "environment": settings.env,
            "hermetic": settings.is_hermetic,
            "replay_mode": settings.replay_mode,
            "paypal_execution": settings.paypal_execution,
            "capacity": guard.capacity_mode(monotonic_ms()).value,
        }


def _register_policy_routes(app: FastAPI, settings: Settings) -> None:
    """Register the public policy endpoint."""

    @app.get("/api/v1/config/policy", tags=["Policy"])
    async def get_active_policy() -> dict[str, Any]:
        """Return public policy limits.

        Deliberately unauthenticated: safety rests on structural
        limits (velocity, daily cap, escalation), not on hiding thresholds.
        Money is serialised as strings to avoid float rounding.
        """
        policy = settings.policy
        return {
            "currency": policy.currency,
            "refund_auto_limit": str(policy.refund_auto_limit),
            "refund_max_limit": str(policy.refund_max_limit),
            "velocity_max_refunds_24h": policy.velocity_max_refunds_24h,
            "daily_outflow_cap": str(policy.daily_outflow_cap),
            "approval_ttl_seconds": settings.approval.ttl_seconds,
        }


def _paypal_client(settings: Settings) -> PayPalClient:
    """Live sandbox calls when configured, otherwise recorded sandbox responses."""
    if settings.paypal_execution == "sandbox":
        return LivePayPalClient(
            settings.paypal_client_id, settings.paypal_client_secret
        )
    return RecordedPayPalClient.from_directory(RECORDINGS_DIR)


def _install_gateway(
    app: FastAPI, settings: Settings, paypal: PayPalClient | None
) -> None:
    """Build the ledger, passkey registry and gateway, and add their routes."""
    database = Path(settings.sqlite_db_path)
    database.parent.mkdir(parents=True, exist_ok=True)
    secret = settings.audit_hmac_key.get_secret_value().encode("utf-8")
    ledger = Ledger(database, secret, settings.policy, settings.paypal_merchant_id)
    passkeys = PasskeyRegistry(database, settings.passkey_rp_id, settings.public_origin)
    app.state.gateway = Gateway(
        ledger=ledger,
        passkeys=passkeys,
        paypal=paypal or _paypal_client(settings),
        policy=settings.policy,
        screening=settings.screening,
        secret=secret,
        approval_ttl_seconds=settings.approval.ttl_seconds,
    )
    app.include_router(gateway_router)


def create_app(
    settings: Settings | None = None, paypal: PayPalClient | None = None
) -> FastAPI:
    """Create and configure the FastAPI application."""
    settings = settings or get_settings()
    is_prod = settings.env == "production"

    app = FastAPI(
        title="AgentCoSign",
        description=(
            "Dual control for AI agents that move money on PayPal: "
            "propose, decide, co-sign, prove"
        ),
        version=__version__,
        docs_url=None,  # served by register_site with a page-specific policy
        redoc_url=None,
        lifespan=lifespan,
    )
    guard = _install_traffic_protection(app, settings)
    _install_gateway(app, settings, paypal)
    _install_security_headers(app)
    register_site(app, docs_enabled=not is_prod)
    _register_system_routes(app, settings, guard)
    _register_policy_routes(app, settings)
    return app


app = create_app()

if __name__ == "__main__":
    import uvicorn

    _settings = get_settings()
    uvicorn.run(
        "agentcosign.main:app",
        host=_settings.host,
        port=_settings.port,
        reload=_settings.debug,
    )
