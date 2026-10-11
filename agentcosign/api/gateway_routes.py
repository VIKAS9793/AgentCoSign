"""Gateway routes: agents propose, approvers co-sign with passkeys.

Three separate principals, never interchangeable:

* the agent, with the agent key, can only propose and read status;
* the operator, with the operator key, can only enrol approver passkeys;
* the approver signs each decision with a passkey, and opens a short session
  with the same passkey to see what is waiting.
"""

import hmac
from typing import Annotated, Any

from fastapi import APIRouter, Depends, HTTPException, Path, Request, Response, status

from agentcosign.core.config import Settings
from agentcosign.core.exceptions import FailClosedError
from agentcosign.schemas.gateway import (
    REQUEST_ID,
    CredentialIn,
    EnrolFinishIn,
    EnrolStartIn,
    IntentIn,
    OutcomeOut,
    ProposalIn,
)
from agentcosign.services.gateway import Gateway, Outcome

router = APIRouter(tags=["Gateway"])
RequestId = Annotated[str, Path(pattern=REQUEST_ID)]


def _gateway(request: Request) -> Gateway:
    gateway: Gateway | None = getattr(request.app.state, "gateway", None)
    if gateway is None:
        raise HTTPException(
            status.HTTP_503_SERVICE_UNAVAILABLE, "Gateway is not ready."
        )
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
    token = request.headers.get("x-approver-session", "")
    if not token or not _gateway(request).session_is_valid(token):
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Approver session required.")


def _refused(exc: FailClosedError) -> HTTPException:
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
    try:
        token = _gateway(request).open_session(body.credential)
    except FailClosedError as exc:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, exc.message) from exc
    return {"session": token}


@router.get("/v1/approvals", dependencies=[Depends(require_approver_session)])
async def pending_approvals(request: Request) -> list[dict[str, str | list[str]]]:
    """Requests waiting for a person, oldest first."""
    return await _gateway(request).pending()


@router.post(
    "/v1/approvals/{request_id}/options",
    dependencies=[Depends(require_approver_session)],
)
async def approval_options(
    request_id: RequestId, body: IntentIn, request: Request
) -> Response:
    """Passkey challenge bound to this exact pending request and intent."""
    try:
        return _json(_gateway(request).approval_options(request_id, body.intent))
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
