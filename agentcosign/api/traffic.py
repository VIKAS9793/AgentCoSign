"""Routes that belong to traffic protection: reviewer session and calibration."""

from typing import Any

from fastapi import APIRouter, HTTPException, Request, Response, status

from agentcosign.api.protection import (
    REVIEWER_COOKIE,
    ClientContext,
    monotonic_ms,
    token_matches,
)
from agentcosign.core.config import Settings
from agentcosign.core.limits import TrafficGuard
from agentcosign.schemas.reviewer import ReviewerSessionRequest

COOKIE_MAX_AGE_SECONDS = 15 * 24 * 60 * 60  # long enough for a review period

router = APIRouter(tags=["Traffic"])


def _guard(request: Request) -> TrafficGuard:
    guard: TrafficGuard = request.app.state.guard
    return guard


def _settings(request: Request) -> Settings:
    settings: Settings = request.app.state.settings
    return settings


@router.get("/api/v1/diagnostics/client")
async def client_diagnostics(request: Request) -> dict[str, Any]:
    """Show how the service identifies the caller, for proxy calibration.

    Returns only the caller's own resolved address group and how many forwarding
    entries arrived, so the operator can confirm the trusted hop count against the
    real network path.
    """
    context: ClientContext = request.state.client
    forwarded = request.headers.get("x-forwarded-for", "")
    entries = [e for e in forwarded.split(",") if e.strip()]
    return {
        "client_key": context.key,
        "tier": context.tier.value,
        "forwarded_for_entries": len(entries),
        "trusted_proxy_hops": _settings(request).limits.trusted_proxy_hops,
    }


@router.post("/api/v1/reviewer/session")
async def start_reviewer_session(
    request: Request, body: ReviewerSessionRequest, response: Response
) -> dict[str, bool]:
    """Exchange the shared reviewer token for an HttpOnly cookie.

    Nothing is gated behind this: it only raises live-call quotas. Wrong tokens
    count as strikes so guessing is blocked quickly.
    """
    settings = _settings(request)
    secret = settings.reviewer_token.get_secret_value()
    if not secret:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Reviewer sessions are off.")
    if not token_matches(body.token.get_secret_value(), secret):
        context: ClientContext = request.state.client
        _guard(request).penalize(context.key, monotonic_ms())
        raise HTTPException(status.HTTP_403_FORBIDDEN, "That token is not valid.")
    response.set_cookie(
        REVIEWER_COOKIE,
        secret,
        max_age=COOKIE_MAX_AGE_SECONDS,
        httponly=True,
        secure=settings.env in ("demo", "production"),
        samesite="strict",
    )
    return {"reviewer": True}
