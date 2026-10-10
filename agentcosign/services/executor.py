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
from dataclasses import dataclass
from datetime import datetime

from agentcosign.clients.paypal import JsonObject, PayPalClient
from agentcosign.core.exceptions import PayPalClientError
from agentcosign.models.domain import Decision, ToolAction
from agentcosign.models.policy import OrderEvidence, Proposal
from agentcosign.services.evidence import fetch_evidence
from agentcosign.services.ledger import Ledger, ProposalState
from agentcosign.services.policy_engine import check_execution_drift


@dataclass(frozen=True, slots=True)
class Execution:
    """What happened when the gateway tried to execute a proposal."""

    request_id: str
    state: ProposalState
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
        current = await fetch_evidence(client, decided.order_id, now=now, secret=secret)
    except PayPalClientError:
        return Execution(request_id, ProposalState.EXECUTING, "evidence_unavailable")
    drift = check_execution_drift(decided, current)
    if drift is not None and drift.decision is Decision.DENY:
        await _move(
            ledger,
            request_id,
            ProposalState.FAILED,
            now,
            {"reason": "evidence_changed"},
        )
        return Execution(request_id, ProposalState.FAILED, "evidence_changed")
    if drift is not None:
        await _move(
            ledger,
            request_id,
            ProposalState.PENDING_APPROVAL,
            now,
            {"reason": "state_drift"},
        )
        return Execution(request_id, ProposalState.PENDING_APPROVAL, "state_drift")
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
