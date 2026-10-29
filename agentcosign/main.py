"""AgentCoSign — Main Application Entrypoint.

FastAPI application factory for the policy gateway.
"""

import asyncio
import logging
import sqlite3
from collections.abc import AsyncGenerator, Awaitable, Callable
from contextlib import asynccontextmanager
from typing import Any

from fastapi import FastAPI, Request, Response

from agentcosign import __version__
from agentcosign.api.gateway_routes import router as gateway_router
from agentcosign.api.protection import ProtectionMiddleware, monotonic_ms
from agentcosign.api.site import register_site
from agentcosign.api.traffic import router as traffic_router
from agentcosign.clients.paypal import PayPalClient
from agentcosign.core import trace
from agentcosign.core.config import Settings, get_settings
from agentcosign.core.exceptions import AgentCoSignError
from agentcosign.core.limits import TrafficGuard
from agentcosign.services.gateway import Gateway
from agentcosign.services.scenarios import ScenarioRunner
from agentcosign.services.workspaces import Workspaces
from agentcosign.wiring import (
    build_assistant,
    build_gateway,
    build_workspaces,
    paypal_client,
)

logger = logging.getLogger("agentcosign")

EXPIRY_INTERVAL_SECONDS = 30

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


async def _expire_forever(workspaces: Workspaces[Gateway]) -> None:
    """Run the timed work for the owner's workspace and every open visitor's one."""
    while True:
        await asyncio.sleep(EXPIRY_INTERVAL_SECONDS)
        await _tend_all(workspaces)


async def _tend_all(workspaces: Workspaces[Gateway]) -> None:
    """One pass of timed work, each workspace reporting into its own steps."""
    for gateway, record in workspaces.tended():
        token = trace.use(record)
        try:
            with trace.automatic():
                await _tend(gateway)
        finally:
            trace.reset(token)


def _timed_jobs(gateway: Gateway) -> list[tuple[str, Callable[[], Awaitable[object]]]]:
    """The timed work, in order, each with the words the step record uses."""
    return [
        ("approval expiry", lambda: asyncio.to_thread(gateway.expire)),
        ("on-hold refunds due", gateway.release_due),
        ("refunds pending at PayPal", gateway.settle_pending),
        ("refunds with no PayPal answer", gateway.finish_unfinished),
        ("reconciliation with PayPal", gateway.reconcile_if_due),
        ("saved record check", gateway.check_records_if_due),
    ]


async def _tend(gateway: Gateway) -> None:
    """Expire approvals left waiting past the window, releasing reservations,
    finish executions whose PayPal answer was lost, and reconcile with PayPal.

    One failing job never stops the others. When any job reported steps, one
    closing step says they came from the app's own timed work, not the visitor.
    """
    ran: list[str] = []
    for name, job in _timed_jobs(gateway):
        before = trace.position()
        try:
            await job()
        except (AgentCoSignError, sqlite3.Error):
            logger.exception("%s failed; will retry", name)
        if trace.position() != before:
            ran.append(name)
    if ran:
        trace.emit(
            "automatic",
            "Automatic check: the steps above ran on their own",
            ran=", ".join(ran),
        )


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncGenerator[None, None]:
    """Log the active run mode and run approval expiry in the background."""
    settings: Settings = app.state.settings
    expiry = asyncio.create_task(_expire_forever(app.state.workspaces))
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
        path = request.url.path
        if path.startswith(("/api", "/v1")):
            # refund data and decisions are never kept in a browser cache
            response.headers["Cache-Control"] = "no-store"
        elif path.startswith("/assets/"):
            # unversioned files are always revalidated, never guessed fresh
            response.headers["Cache-Control"] = "no-cache"
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
            "ai_models": "live" if settings.has_live_models else "off",
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
            "approval_open_seconds": settings.approval.open_seconds,
        }


def _install_gateway(
    app: FastAPI, settings: Settings, paypal: PayPalClient | None, guard: TrafficGuard
) -> None:
    """Build the gateway and add its routes."""
    shared = paypal or paypal_client(settings)

    def on_quota(model: str, left: int | None, limit: int | None) -> None:
        """Each model's own daily quota, so the demo stops before it runs out."""
        guard.observe_provider_quota(left, limit, model)

    app.state.gateway = build_gateway(settings, shared, on_quota)
    app.state.workspaces = build_workspaces(
        settings, app.state.gateway, shared, on_quota
    )
    app.state.scenarios = ScenarioRunner(
        paypal=shared,
        assistant=build_assistant(settings, on_quota),
        per_day=settings.demo.scenarios_per_workspace_per_day,
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
    _install_gateway(app, settings, paypal, guard)
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
