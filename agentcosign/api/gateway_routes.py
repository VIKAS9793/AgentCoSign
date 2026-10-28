"""Gateway routes: agents propose, approvers co-sign with passkeys.

Three separate principals, never interchangeable:

* the agent, with the agent key, can only propose and read status;
* the operator, with the operator key, can only enrol approver passkeys;
* the approver signs each decision with a passkey, and opens a short session
  with the same passkey to see what is waiting.
"""

import asyncio
import hmac
import json
from decimal import Decimal
from typing import Annotated, Any

from fastapi import (
    APIRouter,
    Depends,
    HTTPException,
    Path,
    Query,
    Request,
    Response,
    status,
)
from fastapi.responses import StreamingResponse

from agentcosign.api.protection import ClientContext, monotonic_ms
from agentcosign.api.site import site_build
from agentcosign.core import trace
from agentcosign.core.config import Settings
from agentcosign.core.exceptions import (
    ConfigurationError,
    FailClosedError,
    LimitReachedError,
    ModelUnavailableError,
    PayPalClientError,
    SignatureError,
)
from agentcosign.core.limits import TrafficGuard, WorkKind
from agentcosign.schemas.gateway import (
    REQUEST_ID,
    CredentialIn,
    DemoMessageIn,
    EnrolFinishIn,
    EnrolStartIn,
    IntentIn,
    OutcomeOut,
    ProposalIn,
)
from agentcosign.services.gateway import Gateway, Outcome
from agentcosign.services.live_updates import change_events
from agentcosign.services.scenarios import BY_KEY, SCENARIOS, ScenarioRunner
from agentcosign.services.workspaces import Workspaces

router = APIRouter(tags=["Gateway"])
RequestId = Annotated[str, Path(pattern=REQUEST_ID)]
ControlIntent = Annotated[str, Path(pattern="^(stop|resume)$")]
TracePosition = Annotated[int, Query(ge=0)]
ScenarioKey = Annotated[str, Path(pattern="^(" + "|".join(BY_KEY) + ")$")]
# PayPal events are small; anything larger is refused before it is parsed
WEBHOOK_MAX_BYTES = 65_536


WORKSPACE_HEADER = "x-workspace"


def _gateway(request: Request) -> Gateway:
    """The gateway for the request's workspace: a visitor's own one in the hosted
    demo, otherwise the owner's. A workspace that has closed (the demo slept or
    restarted) answers 410, so the app can ask the visitor to set up again."""
    workspaces: Workspaces[Gateway] | None = getattr(
        request.app.state, "workspaces", None
    )
    if workspaces is None:
        raise HTTPException(
            status.HTTP_503_SERVICE_UNAVAILABLE, "Gateway is not ready."
        )
    workspace_id = request.headers.get(WORKSPACE_HEADER, "")
    gateway = workspaces.default if not workspace_id else workspaces.get(workspace_id)
    if gateway is None:
        raise HTTPException(status.HTTP_410_GONE, "This test setup has closed.")
    settings: Settings = request.app.state.settings
    if settings.env == "demo":
        # on the hosted demo every step of this request joins the visitor's
        # live record for the technical view
        trace.use(workspaces.trace(workspace_id))
    return gateway


def _bearer(request: Request) -> str:
    header = request.headers.get("authorization", "")
    return header[7:] if header.lower().startswith("bearer ") else ""


def _check_key(request: Request, expected: str) -> None:
    presented = _bearer(request)
    if not presented or not hmac.compare_digest(presented.encode(), expected.encode()):
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Not authorised.")


def require_agent(request: Request) -> None:
    """Only the agent key may propose."""
    settings: Settings = request.app.state.settings
    _check_key(request, settings.agent_api_key.get_secret_value())


def require_operator(request: Request) -> None:
    """Enrolment needs the operator key or a one-time link the operator issued."""
    link = request.headers.get("x-enrolment-token", "")
    if link and _gateway(request).enrolment_token_is_valid(link):
        return
    settings: Settings = request.app.state.settings
    _check_key(request, settings.operator_api_key.get_secret_value())


def require_approver_session(request: Request) -> None:
    """Only an approver who opened a passkey session may list or decide."""
    # a closed workspace is said first (set up again), before any sign-in check
    gateway = _gateway(request)
    token = request.headers.get("x-approver-session", "")
    if not token or not gateway.session_is_valid(token):
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Approver session required.")


def _refused(exc: FailClosedError) -> HTTPException:
    """A fingerprint that cannot be accepted (try again) is told apart from a
    request or control that cannot be decided right now."""
    if isinstance(exc, SignatureError):
        return HTTPException(status.HTTP_403_FORBIDDEN, exc.message)
    return HTTPException(status.HTTP_409_CONFLICT, exc.message)


def _out(outcome: Outcome) -> OutcomeOut:
    return OutcomeOut(
        request_id=outcome.request_id,
        decision=outcome.decision,
        rule_id=outcome.rule_id,
        explanation=outcome.explanation,
        state=outcome.state,
        triggered=list(outcome.triggered),
        replayed=outcome.replayed,
        execution=outcome.execution,
        paypal_id=outcome.paypal_id,
        paypal_status=outcome.paypal_status,
        customer_message=outcome.customer_message,
        update_by=outcome.update_by,
        merchant_reason=outcome.merchant_reason,
    )


def _json(text: str) -> Response:
    return Response(content=text, media_type="application/json")


# ----------------------------------------------------------------------- agent


@router.post("/v1/proposals", dependencies=[Depends(require_agent)])
async def propose(body: ProposalIn, request: Request) -> OutcomeOut:
    """Submit a proposed money movement for a decision."""
    return _out(await _gateway(request).propose(body.to_proposal()))


@router.get("/v1/proposals/{request_id}", dependencies=[Depends(require_agent)])
async def proposal_status(request_id: RequestId, request: Request) -> dict[str, str]:
    """Current state of a proposal (the agent polls; it never waits on a person)."""
    view = await _gateway(request).status_view(request_id)
    if view is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Unknown request id.")
    return view


# ---------------------------------------------------------------- hosted demo


@router.post("/v1/demo/workspaces")
async def open_workspace(request: Request) -> dict[str, str]:
    """Hosted demo only: open a private workspace and a one-time setup token for
    it, so a visitor can set up their own device without our keys."""
    settings: Settings = request.app.state.settings
    if settings.env != "demo":
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Not found.")
    workspaces: Workspaces[Gateway] = request.app.state.workspaces
    client = getattr(request.state, "client", None)
    address = client.key if client is not None else ""
    try:
        workspace_id = await asyncio.to_thread(workspaces.open, address)
    except LimitReachedError as exc:
        raise HTTPException(status.HTTP_429_TOO_MANY_REQUESTS, exc.message) from exc
    gateway = workspaces.get(workspace_id)
    if gateway is None:
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, "Try again.")
    trace.use(workspaces.trace(workspace_id))
    trace.emit(
        "workspace",
        "Private workspace opened",
        workspace=workspace_id[:11] + "...",
        storage="its own SQLite database",
        signing="its own secret, derived from the audit key",
    )
    token = gateway.enrolment_token()
    trace.emit("workspace", "One-time setup token issued (signed, expires soon)")
    return {"workspace": workspace_id, "enrolment": token}


@router.get("/v1/demo/trace", dependencies=[Depends(require_approver_session)])
async def demo_trace(request: Request, after: TracePosition = 0) -> dict[str, Any]:
    """Hosted demo only: the visitor's live step record after ``after``."""
    _demo_only(request)
    workspaces: Workspaces[Gateway] = request.app.state.workspaces
    record = workspaces.trace(request.headers.get(WORKSPACE_HEADER, ""))
    steps = [] if record is None else record.after(after)
    return {
        "steps": [
            {
                "seq": s.seq,
                "at": s.at,
                "layer": s.layer,
                "title": s.title,
                "status": s.status,
                "ms": s.ms,
                "detail": dict(s.detail),
            }
            for s in steps
        ],
        "last": steps[-1].seq if steps else after,
    }


def _demo_only(request: Request) -> Settings:
    settings: Settings = request.app.state.settings
    if settings.env != "demo":
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Not found.")
    return settings


@router.get("/v1/demo/scenarios")
async def list_scenarios(request: Request) -> dict[str, Any]:
    """Hosted demo only: the ready scenarios and the visitor's allowances."""
    settings = _demo_only(request)
    return {
        "scenarios": [
            {"key": s.key, "title": s.title, "shows": s.shows} for s in SCENARIOS
        ],
        "per_day": settings.demo.scenarios_per_workspace_per_day,
        "message_max_chars": settings.demo.message_max_chars,
    }


async def _run_demo(run: Any) -> dict[str, Any]:
    """Run a scenario and say plainly what happened or why nothing did."""
    try:
        result = await run
    except LimitReachedError as exc:
        raise HTTPException(status.HTTP_429_TOO_MANY_REQUESTS, exc.message) from exc
    except ModelUnavailableError as exc:
        raise HTTPException(
            status.HTTP_503_SERVICE_UNAVAILABLE,
            "The AI assistant isn't available right now. No money moved.",
        ) from exc
    except PayPalClientError as exc:
        raise HTTPException(
            status.HTTP_502_BAD_GATEWAY,
            "Couldn't create the test order. No money moved. Try again.",
        ) from exc
    outcome = result.outcome
    return {
        "order_id": result.order_id,
        "customer": result.customer,
        "amount": None if result.amount is None else str(result.amount),
        "outcome": None if outcome is None else _out(outcome).model_dump(),
        "caught": result.caught,
    }


def _workspace_key(request: Request) -> str:
    return request.headers.get(WORKSPACE_HEADER, "") or "owner"


# what a visitor is told when a test run cannot start, safe state first
LIVE_RUN_REFUSED = {
    "client_quota": (
        "No money moved. You've run several tests in a row: wait a few minutes, "
        "then try again."
    ),
    "daily_budget": "No money moved. Today's live test runs are used up: try later.",
    "provider_exhausted": (
        "No money moved. Today's live test runs are used up: try later."
    ),
}


def _spend_live_run(request: Request, runner: ScenarioRunner) -> None:
    """A test run spends live model calls: it starts only within the visitor's own
    allowance, their rate and today's shared budget, and nothing is created when
    it cannot start."""
    try:
        runner.check_allowance(_workspace_key(request))
    except LimitReachedError as exc:
        raise HTTPException(status.HTTP_429_TOO_MANY_REQUESTS, exc.message) from exc
    settings: Settings = request.app.state.settings
    if not settings.has_live_models:
        return  # no live models: nothing to spend, the run says so itself
    guard: TrafficGuard = request.app.state.guard
    context: ClientContext = request.state.client
    decision = guard.request_live(
        context.key, context.tier, WorkKind.LIVE_LLM, monotonic_ms()
    )
    if decision.live:
        return
    trace.emit(
        "demo",
        "Test run not started: live model budget used up",
        trace.WARN,
        reason=decision.reason,
        safe="No money moved: no order was created",
    )
    raise HTTPException(
        status.HTTP_429_TOO_MANY_REQUESTS,
        LIVE_RUN_REFUSED.get(decision.reason, LIVE_RUN_REFUSED["daily_budget"]),
    )


@router.post(
    "/v1/demo/scenarios/{key}", dependencies=[Depends(require_approver_session)]
)
async def run_scenario(key: ScenarioKey, request: Request) -> dict[str, Any]:
    """Hosted demo only: run one ready scenario in the visitor's own workspace."""
    _demo_only(request)
    runner: ScenarioRunner = request.app.state.scenarios
    gateway = _gateway(request)
    _spend_live_run(request, runner)
    run = runner.run_ready(gateway, _workspace_key(request), key)
    return await _run_demo(run)


@router.post("/v1/demo/messages", dependencies=[Depends(require_approver_session)])
async def run_own_message(body: DemoMessageIn, request: Request) -> dict[str, Any]:
    """Hosted demo only: a visitor's own customer message, on a fresh test order."""
    settings = _demo_only(request)
    if len(body.text) > settings.demo.message_max_chars:
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_CONTENT, "That message is too long."
        )
    runner: ScenarioRunner = request.app.state.scenarios
    gateway = _gateway(request)
    _spend_live_run(request, runner)
    run = runner.run_own(gateway, _workspace_key(request), body.text)
    return await _run_demo(run)


# ----------------------------------------------------------------- enrolment


@router.post("/v1/approver/enrolment/options", dependencies=[Depends(require_operator)])
async def enrolment_options(body: EnrolStartIn, request: Request) -> Response:
    """Start enrolling an approver passkey."""
    try:
        return _json(_gateway(request).enrolment_options(body.approver))
    except FailClosedError as exc:
        raise _refused(exc) from exc


@router.post("/v1/approver/enrolment", dependencies=[Depends(require_operator)])
async def enrol(body: EnrolFinishIn, request: Request) -> dict[str, str]:
    """Finish enrolling an approver passkey."""
    try:
        _gateway(request).enrol(body.approver, body.credential)
    except FailClosedError as exc:
        raise _refused(exc) from exc
    return {"approver": body.approver, "status": "enrolled"}


# ------------------------------------------------------------------- approver


@router.post("/v1/approver/session/options")
async def session_options(request: Request) -> Response:
    """Passkey challenge for opening an approver session."""
    try:
        return _json(_gateway(request).session_options())
    except FailClosedError as exc:
        raise _refused(exc) from exc


@router.post("/v1/approver/session")
async def open_session(body: CredentialIn, request: Request) -> dict[str, str]:
    """Verify the approver's passkey and return a short-lived session token."""
    gateway = _gateway(request)
    try:
        token, approver = gateway.open_session(body.credential)
    except FailClosedError as exc:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, exc.message) from exc
    return {
        "session": token,
        "approver": approver,
        "account": gateway.account_ending(),
        "session_minutes": str(gateway.session_minutes()),
    }


@router.post(
    "/v1/controls/{intent}/options",
    dependencies=[Depends(require_approver_session)],
)
async def control_options(intent: ControlIntent, request: Request) -> Response:
    """Passkey challenge to stop or resume all refunds."""
    try:
        return _json(_gateway(request).control_options(intent))
    except FailClosedError as exc:
        raise _refused(exc) from exc


@router.post("/v1/controls/{intent}", dependencies=[Depends(require_approver_session)])
async def set_control(
    intent: ControlIntent, body: CredentialIn, request: Request
) -> dict[str, str]:
    """Stop or resume all refunds with a fingerprint signed for that intent."""
    try:
        return await _gateway(request).set_control(intent, body.credential)
    except FailClosedError as exc:
        raise _refused(exc) from exc


@router.post("/v1/webhooks/paypal")
async def paypal_webhook(request: Request) -> dict[str, str]:
    """PayPal events, trusted only after PayPal itself verifies the signature."""
    declared = request.headers.get("content-length", "0")
    if declared.isdigit() and int(declared) > WEBHOOK_MAX_BYTES:
        raise HTTPException(status.HTTP_413_REQUEST_ENTITY_TOO_LARGE, "Too large.")
    raw = await request.body()
    if len(raw) > WEBHOOK_MAX_BYTES:
        raise HTTPException(status.HTTP_413_REQUEST_ENTITY_TOO_LARGE, "Too large.")
    try:
        event = json.loads(raw)
    except ValueError as exc:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "Not JSON.") from exc
    if not isinstance(event, dict):
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "Not an event.")
    headers = {name.lower(): value for name, value in request.headers.items()}
    try:
        outcome = await _gateway(request).handle_webhook(headers, event)
    except ConfigurationError as exc:
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, exc.message) from exc
    except FailClosedError as exc:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, exc.message) from exc
    except PayPalClientError as exc:
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, exc.message) from exc
    return {"status": outcome}


@router.get("/api/v1/audit/head")
async def chain_head(request: Request) -> dict[str, Any]:
    """The latest audit chain entry, for anyone to record independently."""
    return await _gateway(request).chain_head()


@router.get("/v1/approvals/home", dependencies=[Depends(require_approver_session)])
async def merchant_home(request: Request) -> dict[str, Any]:
    """Everything the merchant's home shows, with every state decided here."""
    token = request.headers.get("x-approver-session", "")
    gateway = _gateway(request)
    before = trace.position()
    home = await gateway.home(token)
    # the dashboard's own PayPal reads, named so they are not taken for a run's;
    # a refresh that read nothing from PayPal reports nothing
    reads = sum(1 for step in trace.since(before) if step.layer == "paypal")
    if reads:
        trace.emit(
            "app",
            "Merchant's dashboard refreshed: the PayPal reads above were for it",
            paypal_reads=reads,
        )
    return {**home, "build": site_build()}


@router.get("/v1/approvals/stream", dependencies=[Depends(require_approver_session)])
async def live_updates(request: Request) -> StreamingResponse:
    """Hints that something changed, so the phone re-reads; no money details."""
    gateway = _gateway(request)
    token = request.headers.get("x-approver-session", "")
    # database checks run in a worker thread, never on the event loop
    events = change_events(
        lambda: asyncio.to_thread(gateway.live_version),
        lambda: asyncio.to_thread(gateway.session_is_valid, token),
        asyncio.sleep,
    )
    return StreamingResponse(
        events,
        media_type="text/event-stream",
        headers={"Cache-Control": "no-store", "X-Accel-Buffering": "no"},
    )


@router.get("/v1/approvals/done", dependencies=[Depends(require_approver_session)])
async def done_today(request: Request) -> dict[str, Any]:
    """Requests that ended in the last 24 hours, with the true total."""
    return await _gateway(request).done_page()


@router.get(
    "/v1/approvals/{request_id}/history",
    dependencies=[Depends(require_approver_session)],
)
async def request_history(request_id: RequestId, request: Request) -> dict[str, Any]:
    """One request's record in owner words."""
    found = await _gateway(request).history(request_id)
    if found is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Unknown request id.")
    return found


@router.post(
    "/v1/approvals/{request_id}/options",
    dependencies=[Depends(require_approver_session)],
)
async def approval_options(
    request_id: RequestId, body: IntentIn, request: Request
) -> Response:
    """Passkey challenge bound to this exact pending request and intent."""
    seen = (
        (Decimal(body.seen.paid), Decimal(body.seen.already_refunded))
        if body.seen
        else None
    )
    try:
        options = await _gateway(request).approval_options(
            request_id, body.intent, seen
        )
        return _json(options)
    except FailClosedError as exc:
        raise _refused(exc) from exc


async def _decide(
    request: Request, request_id: str, intent: str, body: Any
) -> OutcomeOut:
    try:
        outcome = await _gateway(request).decide_approval(
            request_id, intent, body.credential
        )
    except FailClosedError as exc:
        raise _refused(exc) from exc
    return _out(outcome)


@router.post(
    "/v1/approvals/{request_id}/approve",
    dependencies=[Depends(require_approver_session)],
)
async def approve(
    request_id: RequestId, body: CredentialIn, request: Request
) -> OutcomeOut:
    """Approve with a passkey signature; the gateway then executes safely."""
    return await _decide(request, request_id, "approve", body)


@router.post(
    "/v1/approvals/{request_id}/reject",
    dependencies=[Depends(require_approver_session)],
)
async def reject(
    request_id: RequestId, body: CredentialIn, request: Request
) -> OutcomeOut:
    """Reject with a passkey signature; the reservation is released."""
    return await _decide(request, request_id, "reject", body)
