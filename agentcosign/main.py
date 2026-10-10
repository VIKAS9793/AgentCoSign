"""AgentCoSign — Main Application Entrypoint.

FastAPI application factory for the policy gateway.
"""

import logging
from collections.abc import AsyncGenerator, Awaitable, Callable
from contextlib import asynccontextmanager
from typing import Any

from fastapi import FastAPI, Request, Response

from agentcosign import __version__
from agentcosign.core.config import Settings, get_settings

logger = logging.getLogger("agentcosign")

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


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncGenerator[None, None]:
    """Fail fast on bad configuration; log the active run mode."""
    settings = get_settings()  # raises ConfigurationError -> process does not start
    logger.info(
        "AgentCoSign v%s env=%s hermetic=%s replay_mode=%s paypal_execution=%s",
        __version__,
        settings.env,
        settings.is_hermetic,
        settings.replay_mode,
        settings.paypal_execution,
    )
    yield
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


def _register_system_routes(app: FastAPI, settings: Settings) -> None:
    """Register the liveness endpoint (also reports the active run modes)."""

    @app.get("/health", tags=["System"])
    async def health_check() -> dict[str, Any]:
        """Liveness + run-mode report for Render monitoring and the demo banner."""
        return {
            "status": "healthy",
            "service": "AgentCoSign",
            "version": __version__,
            "environment": settings.env,
            "hermetic": settings.is_hermetic,
            "replay_mode": settings.replay_mode,
            "paypal_execution": settings.paypal_execution,
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


def create_app() -> FastAPI:
    """Create and configure the FastAPI application."""
    settings = get_settings()
    is_prod = settings.env == "production"

    app = FastAPI(
        title="AgentCoSign",
        description=(
            "Dual control for AI agents that move money on PayPal: "
            "propose, decide, co-sign, prove"
        ),
        version=__version__,
        docs_url=None if is_prod else "/docs",
        redoc_url=None if is_prod else "/redoc",
        lifespan=lifespan,
    )
    _install_security_headers(app)
    _register_system_routes(app, settings)
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
