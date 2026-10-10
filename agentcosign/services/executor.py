"""Carry out an allowed or approved proposal against PayPal, safely.

Before moving money the order is read again and compared with the evidence the
decision was based on. A material change stops the payment; a change of state
(a dispute opened, another refund landed) sends it back for approval.

When PayPal's answer is unknown (timeout, rate limit, server error) the proposal
stays reserved and in progress: retrying with the same request id is safe because
PayPal returns the original result, whereas releasing the reservation could let
the same money be spent twice. Only a definite refusal releases it.
"""

import asyncio
from dataclasses import dataclass, replace
from datetime import datetime

from agentcosign.clients.paypal import JsonObject, PayPalClient
from agentcosign.core.exceptions import FailClosedError, PayPalClientError
from agentcosign.models.domain import Decision, ToolAction
from agentcosign.models.policy import OrderEvidence, Proposal
from agentcosign.services.evidence import fetch_evidence
from agentcosign.services.ledger import Ledger, ProposalState
from agentcosign.services.policy_engine import check_execution_drift


@dataclass(frozen=True, slots=True)
class Execution:
    """What happened when the gateway tried to execute a proposal."""

    request_id: str
    state: ProposalState | None
    reason: str
    paypal_id: str | None = None


async def _move(
    ledger: Ledger,
    request_id: str,
    state: ProposalState,
    now: datetime,
    detail: dict[str, str],
) -> None:
    await asyncio.to_thread(ledger.transition, request_id, state, now, detail)


async def _call_paypal(
    client: PayPalClient, proposal: Proposal, current: OrderEvidence
) -> JsonObject:
    if proposal.tool == ToolAction.CREATE_REFUND.value:
        if current.capture_id is None:
            raise PayPalClientError("the order has no capture to refund")
        return await client.refund_capture(
            current.capture_id, proposal.amount, proposal.currency, proposal.request_id
        )
    if proposal.tool == ToolAction.ACCEPT_DISPUTE_CLAIM.value and proposal.dispute_id:
        return await client.accept_claim(proposal.dispute_id, proposal.request_id)
    raise PayPalClientError(
        "this action cannot be executed yet", {"tool": proposal.tool}
    )


async def _stop_on_drift(
    ledger: Ledger,
    request_id: str,
    decided: OrderEvidence,
    current: OrderEvidence,
    previous_attempts: int,
    now: datetime,
) -> Execution | None:
    """Stop or send back for approval when the order changed since the decision."""
    compared = current
    if previous_attempts > 0:
        # An earlier attempt may already have landed at PayPal. Its refund is not
        # an outside change: resending the same request id returns that result.
        compared = replace(current, refunded_amount=decided.refunded_amount)
    drift = check_execution_drift(decided, compared)
    if drift is None:
        return None
    if drift.decision is Decision.DENY:
        state, reason = ProposalState.FAILED, "evidence_changed"
    else:
        state, reason = ProposalState.PENDING_APPROVAL, "state_drift"
    await _move(ledger, request_id, state, now, {"reason": reason})
    return Execution(request_id, state, reason)


async def execute(
    proposal: Proposal,
    decided: OrderEvidence,
    client: PayPalClient,
    ledger: Ledger,
    *,
    now: datetime,
    secret: bytes,
) -> Execution:
    """Re-check the evidence, then execute idempotently and record the outcome."""
    request_id = proposal.request_id
    try:
        previous = await asyncio.to_thread(ledger.begin_execution, proposal, now)
    except FailClosedError:
        state = await asyncio.to_thread(ledger.state_of, request_id)
        return Execution(request_id, state, "not_executable")
    try:
        current = await fetch_evidence(client, decided.order_id, now=now, secret=secret)
    except PayPalClientError:
        return Execution(request_id, ProposalState.EXECUTING, "evidence_unavailable")
    stopped = await _stop_on_drift(ledger, request_id, decided, current, previous, now)
    if stopped is not None:
        return stopped
    try:
        response = await _call_paypal(client, proposal, current)
    except PayPalClientError as exc:
        if exc.details.get("retryable"):
            return Execution(request_id, ProposalState.EXECUTING, "retry_with_same_id")
        status = str(exc.details.get("status", ""))
        detail = {"reason": "paypal_refused", "paypal_status": status}
        await _move(ledger, request_id, ProposalState.FAILED, now, detail)
        return Execution(request_id, ProposalState.FAILED, "paypal_refused")
    paypal_id = str(response.get("id", ""))
    detail = {"paypal_id": paypal_id, "paypal_status": str(response.get("status", ""))}
    await _move(ledger, request_id, ProposalState.EXECUTED, now, detail)
    return Execution(request_id, ProposalState.EXECUTED, "executed", paypal_id)
