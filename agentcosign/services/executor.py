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
    paypal_status: str | None = None


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


LANDED_STATUSES = ("COMPLETED", "PENDING")
PAYPAL_FAILED = ("FAILED", "CANCELLED")


def _our_refund(proposal: Proposal, current: OrderEvidence) -> tuple[str, str] | None:
    """Our own refund already on the order, found by the stamp we put on it.

    An earlier attempt may have reached PayPal without us seeing the reply. The
    stamp is our request id, so an outside refund of the same amount can never
    be mistaken for ours, and we record ours instead of sending it again.
    """
    if proposal.tool != ToolAction.CREATE_REFUND.value:
        return None
    for tag, refund_id, status in current.tagged_refunds:
        if tag == proposal.request_id and status in LANDED_STATUSES:
            return refund_id, status
    return None


async def _stop_on_drift(
    ledger: Ledger,
    proposal: Proposal,
    decided: OrderEvidence,
    current: OrderEvidence,
    own_landed: bool,
    now: datetime,
) -> Execution | None:
    """Stop or send back for approval when the order changed since the decision."""
    request_id = proposal.request_id
    compared = current
    if own_landed:
        # take out only our own refund; any other new refund is a real change
        others = current.refunded_amount - proposal.amount
        compared = replace(current, refunded_amount=others)
    drift = check_execution_drift(decided, compared)
    if drift is None:
        return None
    if drift.decision is Decision.DENY:
        await _move(
            ledger,
            request_id,
            ProposalState.FAILED,
            now,
            {"reason": "evidence_changed"},
        )
        return Execution(request_id, ProposalState.FAILED, "evidence_changed")
    await asyncio.to_thread(
        ledger.send_back_for_approval,
        request_id,
        current,
        now,
        {"reason": "state_drift"},
        drift,
    )
    return Execution(request_id, ProposalState.PENDING_APPROVAL, "state_drift")


async def _stop_if_over_refundable(
    ledger: Ledger,
    proposal: Proposal,
    current: OrderEvidence,
    own_landed: bool,
    now: datetime,
) -> Execution | None:
    """Refuse a refund that would take the order past what the customer paid.

    Checked against PayPal's figures immediately before sending, whatever was
    decided earlier, so no approval can return more than was paid.
    """
    if proposal.tool != ToolAction.CREATE_REFUND.value:
        return None
    others = current.refunded_amount - (proposal.amount if own_landed else 0)
    if others + proposal.amount <= current.captured_amount:
        return None
    detail = {"reason": "exceeds_refundable"}
    await _move(ledger, proposal.request_id, ProposalState.FAILED, now, detail)
    return Execution(proposal.request_id, ProposalState.FAILED, "exceeds_refundable")


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
        await asyncio.to_thread(ledger.begin_execution, proposal, now)
    except FailClosedError:
        state = await asyncio.to_thread(ledger.state_of, request_id)
        return Execution(request_id, state, "not_executable")
    try:
        current = await fetch_evidence(client, decided.order_id, now=now, secret=secret)
    except PayPalClientError:
        return Execution(request_id, ProposalState.EXECUTING, "evidence_unavailable")
    landed = _our_refund(proposal, current)
    own_landed = landed is not None
    stopped = await _stop_on_drift(
        ledger, proposal, decided, current, own_landed, now
    ) or await _stop_if_over_refundable(ledger, proposal, current, own_landed, now)
    if stopped is not None:
        return stopped
    if landed is not None:
        return await _record_success(ledger, request_id, now, *landed)
    if await asyncio.to_thread(ledger.refunds_stopped):
        # nothing new is sent while stopped; it is retried with the same request
        # id after refunds resume, so it can never go out twice
        return Execution(request_id, ProposalState.EXECUTING, "held_by_stop")
    try:
        response = await _call_paypal(client, proposal, current)
    except PayPalClientError as exc:
        if exc.details.get("retryable"):
            return Execution(request_id, ProposalState.EXECUTING, "retry_with_same_id")
        status = str(exc.details.get("status", ""))
        detail = {"reason": "paypal_refused", "paypal_status": status}
        await _move(ledger, request_id, ProposalState.FAILED, now, detail)
        return Execution(request_id, ProposalState.FAILED, "paypal_refused")
    return await _record_success(
        ledger,
        request_id,
        now,
        str(response.get("id", "")),
        str(response.get("status", "")),
    )


async def _record_success(
    ledger: Ledger, request_id: str, now: datetime, paypal_id: str, status: str
) -> Execution:
    """Record PayPal's answer; a refund it answers as failed moved no money."""
    if status in PAYPAL_FAILED:
        await asyncio.to_thread(
            ledger.mark_failed_by_paypal, request_id, now, paypal_id, status
        )
        return Execution(
            request_id, ProposalState.FAILED, "paypal_failed", paypal_id, status
        )
    await asyncio.to_thread(ledger.mark_executed, request_id, now, paypal_id, status)
    return Execution(request_id, ProposalState.EXECUTED, "executed", paypal_id, status)
