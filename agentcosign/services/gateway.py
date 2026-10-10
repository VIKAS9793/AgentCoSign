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
import secrets
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

from agentcosign.clients.paypal import PayPalClient
from agentcosign.core.config import PolicySettings, ScreeningSettings
from agentcosign.core.exceptions import FailClosedError, PayPalClientError
from agentcosign.models.domain import Decision, ToolAction
from agentcosign.models.policy import OrderEvidence, Proposal, ScreeningVerdict, Signals
from agentcosign.services.evidence import fetch_evidence
from agentcosign.services.executor import execute
from agentcosign.services.ledger import Ledger, ProposalState
from agentcosign.services.passkeys import PasskeyRegistry

SESSION_SECONDS = 600
ENROLMENT_LINK_SECONDS = 600
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
        if (
            recorded.replayed
            or result.decision is not Decision.ALLOW
            or evidence is None
        ):
            return outcome
        return await self._run(proposal, evidence, outcome)

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

    def pending(self) -> list[dict[str, str]]:
        """Requests waiting for a person, facts first."""
        return self._ledger.pending_approvals()

    def expire(self) -> list[str]:
        """Expire approvals that waited longer than the approval window."""
        return self._ledger.expire_due(self._clock(), self._ttl)

    # ------------------------------------------------------------- approvals

    def _pending_digest(self, request_id: str) -> str:
        if self._ledger.state_of(request_id) is not ProposalState.PENDING_APPROVAL:
            raise FailClosedError("this request is not waiting for approval")
        digest = self._ledger.action_digest(request_id)
        if digest is None:
            raise FailClosedError("unknown request id")
        return digest

    def approval_options(self, request_id: str, intent: str) -> str:
        """Passkey challenge bound to this exact pending action and intent."""
        digest = self._pending_digest(request_id)
        return self._passkeys.approval_options(
            request_id, intent, digest, self._clock()
        )

    async def decide_approval(
        self, request_id: str, intent: str, credential: dict[str, Any]
    ) -> Outcome:
        """Apply a passkey-signed approve or reject, then execute if approved."""
        now = self._clock()
        digest = self._pending_digest(request_id)
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
