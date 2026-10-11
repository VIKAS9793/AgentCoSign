"""The gateway: from an agent's proposal to a recorded, approved, executed action.

Order of work for a proposal:

1. Read PayPal's record of the order (evidence). If it cannot be read, the
   decision is made without it, which the policy denies.
2. Decide and reserve in one ledger transaction.
3. Allowed: execute now. Escalated: wait for an approver's passkey. Denied: stop.

An approver approves or rejects with a passkey signature bound to the exact
recorded action. Approval moves the proposal to execution, which re-reads the
order and pays only if nothing material changed.

Screening is not wired into this path yet, so every proposal is decided with
screening reported as unavailable. The policy therefore sends every request to a
person: nothing is allowed automatically until screening exists. That is the
safe direction and is reported honestly in the outcome.
"""

import asyncio
import base64
import hashlib
import hmac
import json
import secrets
from collections.abc import Callable
from dataclasses import dataclass, replace
from datetime import UTC, datetime, timedelta
from typing import Any

from agentcosign.clients.paypal import PayPalClient
from agentcosign.core.config import PolicySettings, ScreeningSettings
from agentcosign.core.exceptions import FailClosedError, PayPalClientError
from agentcosign.models.domain import Decision, ToolAction
from agentcosign.models.policy import OrderEvidence, Proposal, ScreeningVerdict, Signals
from agentcosign.services import customer_copy
from agentcosign.services.approval_view import (
    UNKNOWN_NAME,
    business_reasons,
    decide_by,
    money_view,
    order_facts,
    safety_check,
    unavailable_facts,
)
from agentcosign.services.evidence import fetch_evidence
from agentcosign.services.executor import execute
from agentcosign.services.ledger import Ledger, ProposalState
from agentcosign.services.passkeys import PasskeyRegistry

SESSION_SECONDS = 600
ENROLMENT_LINK_SECONDS = 600
# longer than one PayPal call can take, so a retry never overlaps a live attempt
RETRY_QUIET_SECONDS = 60
VIEW_REQUEST = "approver-session"
VIEW_DIGEST = hashlib.sha256(b"view pending approvals").hexdigest()


@dataclass(frozen=True, slots=True)
class Outcome:
    """What the gateway did with a proposal, safe to return to the caller."""

    request_id: str
    decision: str
    rule_id: str
    explanation: str
    state: str | None
    triggered: tuple[str, ...]
    replayed: bool = False
    execution: str | None = None
    paypal_id: str | None = None
    paypal_status: str | None = None
    customer_message: str | None = None
    update_by: str | None = None


def _utc_now() -> datetime:
    return datetime.now(UTC)


def _sign(secret: bytes, purpose: str, body: str) -> str:
    mac = hmac.new(secret, f"{purpose}|{body}".encode(), hashlib.sha256)
    return base64.urlsafe_b64encode(mac.digest()).decode().rstrip("=")


def issue_token(
    secret: bytes, purpose: str, subject: str, lifetime: int, now: datetime
) -> str:
    """A signed, expiring token for one purpose (session or enrolment link)."""
    expires = int((now + timedelta(seconds=lifetime)).timestamp())
    body = f"{subject}.{expires}"
    return f"{body}.{_sign(secret, purpose, body)}"


def token_is_valid(secret: bytes, purpose: str, token: str, now: datetime) -> bool:
    """True for an unexpired token signed for this purpose with this secret."""
    parts = token.split(".")
    if len(parts) != 3 or not parts[1].isdigit():
        return False
    body = f"{parts[0]}.{parts[1]}"
    fresh = int(parts[1]) > int(now.timestamp())
    return fresh and hmac.compare_digest(parts[2], _sign(secret, purpose, body))


class Gateway:
    """Coordinates evidence, decision, approval and execution."""

    def __init__(
        self,
        *,
        ledger: Ledger,
        passkeys: PasskeyRegistry,
        paypal: PayPalClient,
        policy: PolicySettings,
        screening: ScreeningSettings,
        secret: bytes,
        approval_ttl_seconds: int,
        clock: Callable[[], datetime] = _utc_now,
    ) -> None:
        self._ledger = ledger
        self._passkeys = passkeys
        self._paypal = paypal
        self._policy = policy
        self._screening = screening
        self._secret = secret
        self._ttl = approval_ttl_seconds
        self._clock = clock

    # ------------------------------------------------------------- proposals

    async def _evidence(
        self, proposal: Proposal, now: datetime
    ) -> OrderEvidence | None:
        if proposal.tool == ToolAction.PAYOUT.value or not proposal.order_id:
            return None
        try:
            return await fetch_evidence(
                self._paypal, proposal.order_id, now=now, secret=self._secret
            )
        except PayPalClientError:
            return None  # decided without evidence, which the policy denies

    async def propose(self, proposal: Proposal) -> Outcome:
        """Decide a proposal and execute it at once if the policy allows."""
        now = self._clock()
        if await asyncio.to_thread(self._ledger.state_of, proposal.request_id) is None:
            existing = await asyncio.to_thread(
                self._ledger.same_paused_request, proposal
            )
            if existing is not None:
                return await self._already_paused(existing, proposal, now)
        evidence = await self._evidence(proposal, now)
        signals = Signals(screening=ScreeningVerdict.UNAVAILABLE)
        recorded = await asyncio.to_thread(
            self._ledger.decide, proposal, evidence, signals, self._screening, now
        )
        result = recorded.result
        outcome = Outcome(
            request_id=proposal.request_id,
            decision=result.decision.value,
            rule_id=result.rule_id,
            explanation=result.explanation,
            state=recorded.state.value if recorded.state else None,
            triggered=result.triggered_rules,
            replayed=recorded.replayed,
        )
        if result.decision is Decision.PAUSE and result.release_at is not None:
            name = await self._first_name(proposal.order_id)
            hours = customer_copy.hours_until(result.release_at, now)
            outcome = replace(
                outcome,
                customer_message=customer_copy.paused(
                    name, proposal.order_id or "", f"{proposal.amount:.2f}", hours
                ),
                update_by=result.release_at.isoformat(),
            )
        if (
            recorded.replayed
            or result.decision is not Decision.ALLOW
            or evidence is None
        ):
            return outcome
        return await self._run(proposal, evidence, outcome)

    async def _first_name(self, order_id: str | None) -> str | None:
        """The customer's first name from PayPal, read live and never stored."""
        if not order_id:
            return None
        try:
            facts = order_facts(await self._paypal.get_order(order_id))
        except PayPalClientError:
            return None
        return customer_copy.first_name(facts["customer"], UNKNOWN_NAME)

    async def _already_paused(
        self, existing: str, proposal: Proposal, now: datetime
    ) -> Outcome:
        """A repeat ask for a paused refund gets the same pause, not a new request."""
        timing = await asyncio.to_thread(self._ledger.timing, existing)
        release = datetime.fromisoformat(timing[1]) if timing and timing[1] else now
        hours = customer_copy.hours_until(release, now)
        return Outcome(
            request_id=existing,
            decision=Decision.PAUSE.value,
            rule_id="R5",
            explanation="The same refund is already paused; nothing new was recorded.",
            state=ProposalState.PAUSED.value,
            triggered=("R5",),
            replayed=True,
            customer_message=customer_copy.still_paused(proposal.order_id or "", hours),
            update_by=release.isoformat(),
        )

    async def release_due(self) -> list[str]:
        """Decide paused requests again once their release time has come.

        Facts are read fresh from PayPal. If PayPal cannot be reached the request
        stays paused and is tried again, so an outage never turns into a refusal.
        """
        now = self._clock()
        released: list[str] = []
        for request_id in await asyncio.to_thread(self._ledger.releases_due, now):
            loaded = await asyncio.to_thread(self._ledger.load, request_id)
            if loaded is None:
                continue
            proposal = loaded[0]
            evidence = await self._evidence(proposal, now)
            if evidence is None:
                continue
            signals = Signals(screening=ScreeningVerdict.UNAVAILABLE)
            recorded = await asyncio.to_thread(
                self._ledger.release,
                request_id,
                evidence,
                signals,
                self._screening,
                now,
            )
            if recorded.result.decision is Decision.ALLOW:
                await execute(
                    proposal,
                    evidence,
                    self._paypal,
                    self._ledger,
                    now=now,
                    secret=self._secret,
                )
            released.append(request_id)
        return released

    async def settle_pending(self) -> list[str]:
        """Re-read refunds PayPal reported as pending and record how they ended.

        Uses the order read, which lists every refund with our stamp and its
        current status. A refund that failed or was cancelled later is marked
        failed, so neither the merchant nor the customer is told money moved.
        """
        now = self._clock()
        changed: list[str] = []
        for request_id in await asyncio.to_thread(self._ledger.unsettled):
            loaded = await asyncio.to_thread(self._ledger.load, request_id)
            if loaded is None:
                continue
            evidence = await self._evidence(loaded[0], now)
            if evidence is None:
                continue  # PayPal unreachable: try again on the next pass
            for tag, refund_id, status in evidence.tagged_refunds:
                if tag == request_id and status != "PENDING":
                    await asyncio.to_thread(
                        self._ledger.record_paypal_status,
                        request_id,
                        refund_id,
                        status,
                        now,
                    )
                    changed.append(request_id)
        return changed

    async def status_view(self, request_id: str) -> dict[str, str] | None:
        """The state of a proposal and the exact line to tell the customer."""
        timing = await asyncio.to_thread(self._ledger.timing, request_id)
        loaded = await asyncio.to_thread(self._ledger.load, request_id)
        if timing is None:
            return None
        state, release, changed = timing
        proposal = loaded[0] if loaded else None
        order = (proposal.order_id if proposal else None) or ""
        amount = f"{proposal.amount:.2f}" if proposal else "0.00"
        view = {"request_id": request_id, "state": state.value}
        moment = self._update_moment(state, release, changed)
        if moment is not None:
            view["update_by"] = moment.isoformat()
        view["customer_message"] = await self._customer_line(
            state, request_id, order, amount, moment
        )
        return view

    def _update_moment(
        self, state: ProposalState, release: str, changed: str
    ) -> datetime | None:
        """When the customer will next hear something, if a time was promised."""
        if state is ProposalState.PAUSED:
            return datetime.fromisoformat(release) if release else self._clock()
        if state is ProposalState.PENDING_APPROVAL:
            return datetime.fromisoformat(decide_by(changed, self._ttl))
        return None

    async def _customer_line(
        self,
        state: ProposalState,
        request_id: str,
        order: str,
        amount: str,
        moment: datetime | None,
    ) -> str:
        hours = customer_copy.hours_until(moment, self._clock()) if moment else 1
        match state:
            case ProposalState.PAUSED:
                return customer_copy.still_paused(order, hours)
            case ProposalState.PENDING_APPROVAL:
                return customer_copy.with_team(order, hours)
            case ProposalState.EXECUTING:
                return customer_copy.sending(order)
            case ProposalState.EXECUTED:
                name = await self._first_name(order)
                return customer_copy.sent(name, order, amount)
        paypal = await asyncio.to_thread(self._ledger.paypal_status, request_id)
        if state is ProposalState.FAILED and paypal in ("FAILED", "CANCELLED"):
            return customer_copy.failed_late(order)
        return customer_copy.declined(order)

    async def _run(
        self, proposal: Proposal, evidence: OrderEvidence, outcome: Outcome
    ) -> Outcome:
        execution = await execute(
            proposal,
            evidence,
            self._paypal,
            self._ledger,
            now=self._clock(),
            secret=self._secret,
        )
        return Outcome(
            request_id=outcome.request_id,
            decision=outcome.decision,
            rule_id=outcome.rule_id,
            explanation=outcome.explanation,
            state=execution.state.value if execution.state else outcome.state,
            triggered=outcome.triggered,
            execution=execution.reason,
            paypal_id=execution.paypal_id,
            paypal_status=execution.paypal_status,
        )

    def status(self, request_id: str) -> str | None:
        """Current lifecycle state of a proposal, or None if unknown."""
        state = self._ledger.state_of(request_id)
        return state.value if state else None

    # ------------------------------------------------------------- approvers

    def enrolment_options(self, approver: str) -> str:
        """Start enrolling an approver passkey (the caller must be the operator)."""
        return self._passkeys.registration_options(approver, self._clock())

    def enrol(self, approver: str, credential: dict[str, Any]) -> str:
        """Finish enrolling an approver passkey."""
        return self._passkeys.register(approver, credential, self._clock())

    def session_options(self) -> str:
        """Passkey challenge for opening an approver session to view requests."""
        return self._passkeys.approval_options(
            VIEW_REQUEST, "view", VIEW_DIGEST, self._clock()
        )

    def open_session(self, credential: dict[str, Any]) -> str:
        """Verify the approver's passkey and issue a short-lived session token."""
        now = self._clock()
        approver = self._passkeys.verify_approval(
            VIEW_REQUEST, "view", VIEW_DIGEST, credential, now
        )
        return self._token(
            "approver-session", approver.credential_digest, SESSION_SECONDS
        )

    def session_is_valid(self, token: str) -> bool:
        """True for an unexpired, untampered approver session token."""
        return self._token_is_valid("approver-session", token)

    def enrolment_link_token(self) -> str:
        """A short-lived token that lets one phone enrol an approver passkey.

        Issued on the operator's own machine, so the operator key never has to be
        typed into the phone.
        """
        return self._token("enrolment", secrets.token_hex(8), ENROLMENT_LINK_SECONDS)

    def enrolment_token_is_valid(self, token: str) -> bool:
        """True for an unexpired, untampered enrolment link token."""
        return self._token_is_valid("enrolment", token)

    def _token(self, purpose: str, subject: str, lifetime: int) -> str:
        return issue_token(self._secret, purpose, subject, lifetime, self._clock())

    def _token_is_valid(self, purpose: str, token: str) -> bool:
        return token_is_valid(self._secret, purpose, token, self._clock())

    async def pending(self) -> list[dict[str, str | list[str]]]:
        """Everything the merchant should see: requests waiting for them, paused
        refunds, and refunds still waiting for PayPal. Customer names are read
        live from PayPal and never stored."""
        views: list[dict[str, str | list[str]]] = []
        views += await self._waiting_views()
        views += await self._paused_views()
        views += await self._sending_views()
        views += await self._late_failure_views()
        return views

    async def _late_failure_views(self) -> list[dict[str, str | list[str]]]:
        views: list[dict[str, str | list[str]]] = []
        for item in await asyncio.to_thread(self._ledger.late_failures, self._clock()):
            facts = await self._live_facts(item["order_id"])
            views.append(
                {
                    **self._basics(item, "failed_late"),
                    "customer": facts["customer"],
                    "failed_at": item["updated_at"],
                }
            )
        return views

    async def _live_facts(self, order_id: str) -> dict[str, str]:
        if not order_id:
            return unavailable_facts()
        try:
            return order_facts(await self._paypal.get_order(order_id))
        except PayPalClientError:
            return unavailable_facts()  # the card still shows the order and amount

    @staticmethod
    def _basics(item: dict[str, str], status: str) -> dict[str, str | list[str]]:
        return {
            "status": status,
            "request_id": item["request_id"],
            "tool": item["tool"],
            "amount": item["amount"],
            "currency": item["currency"],
            "order_id": item["order_id"],
        }

    async def _waiting_views(self) -> list[dict[str, str | list[str]]]:
        views: list[dict[str, str | list[str]]] = []
        for item in await asyncio.to_thread(self._ledger.pending_approvals):
            facts = await self._live_facts(item["order_id"])
            triggered = json.loads(item["triggered"] or "[]")
            views.append(
                {
                    **self._basics(item, "waiting"),
                    "reasons": business_reasons(triggered, self._policy),
                    "safety_check": safety_check(triggered),
                    **money_view(facts, item["amount"]),
                    "asked_at": item["created_at"],
                    "decide_by": decide_by(item["updated_at"], self._ttl),
                    **facts,
                }
            )
        return views

    async def _paused_views(self) -> list[dict[str, str | list[str]]]:
        limit = self._policy.velocity_max_refunds_24h
        views: list[dict[str, str | list[str]]] = []
        for item in await asyncio.to_thread(self._ledger.paused, self._clock()):
            facts = await self._live_facts(item["order_id"])
            many = int(item["requests_24h"]) >= 2 * limit
            views.append(
                {
                    **self._basics(item, "paused"),
                    "customer": facts["customer"],
                    "release_at": item["release_at"],
                    # this request would be the next refund after those counted
                    "count_in_window": str(int(item["refunds_24h"]) + 1),
                    "many_requests": "yes" if many else "no",
                }
            )
        return views

    async def _sending_views(self) -> list[dict[str, str | list[str]]]:
        return [
            {**self._basics(item, "sending"), "last_try": item["updated_at"]}
            for item in await asyncio.to_thread(self._ledger.sending)
        ]

    async def finish_unfinished(self) -> list[str]:
        """Retry executions whose PayPal answer never arrived, with the same id.

        PayPal returns the original result for a repeated request id, and the
        executor re-reads the order first, so a retry cannot refund twice.
        """
        finished: list[str] = []
        stale = await asyncio.to_thread(
            self._ledger.unfinished_executions, self._clock(), RETRY_QUIET_SECONDS
        )
        for request_id in stale:
            loaded = await asyncio.to_thread(self._ledger.load, request_id)
            if loaded is None or loaded[1] is None:
                continue
            execution = await execute(
                loaded[0],
                loaded[1],
                self._paypal,
                self._ledger,
                now=self._clock(),
                secret=self._secret,
            )
            if execution.state is not ProposalState.EXECUTING:
                finished.append(request_id)
        return finished

    def expire(self) -> list[str]:
        """Expire approvals that waited longer than the approval window."""
        return self._ledger.expire_due(self._clock(), self._ttl)

    # ------------------------------------------------------------- approvals

    def _pending_digest(self, request_id: str, intent: str) -> str:
        state = self._ledger.state_of(request_id)
        declining_a_pause = state is ProposalState.PAUSED and intent == "reject"
        if state is not ProposalState.PENDING_APPROVAL and not declining_a_pause:
            raise FailClosedError("this request is not waiting for approval")
        digest = self._ledger.action_digest(request_id)
        if digest is None:
            raise FailClosedError("unknown request id")
        return digest

    def approval_options(self, request_id: str, intent: str) -> str:
        """Passkey challenge bound to this exact pending action and intent."""
        digest = self._pending_digest(request_id, intent)
        return self._passkeys.approval_options(
            request_id, intent, digest, self._clock()
        )

    async def decide_approval(
        self, request_id: str, intent: str, credential: dict[str, Any]
    ) -> Outcome:
        """Apply a passkey-signed approve or reject, then execute if approved."""
        now = self._clock()
        digest = self._pending_digest(request_id, intent)
        approver = self._passkeys.verify_approval(
            request_id, intent, digest, credential, now
        )
        detail = {
            "assurance": "passkey",
            "approver": approver.name,
            "passkey": approver.credential_digest,
        }
        target = (
            ProposalState.EXECUTING if intent == "approve" else ProposalState.DENIED
        )
        await asyncio.to_thread(
            self._ledger.transition, request_id, target, now, detail
        )
        loaded = await asyncio.to_thread(self._ledger.load, request_id)
        base = Outcome(
            request_id=request_id,
            decision="approved" if intent == "approve" else "rejected",
            rule_id="",
            explanation=f"{'Approved' if intent == 'approve' else 'Rejected'} by "
            f"{approver.name} with a passkey.",
            state=target.value,
            triggered=(),
        )
        if intent != "approve" or loaded is None or loaded[1] is None:
            return base
        return await self._run(loaded[0], loaded[1], base)
