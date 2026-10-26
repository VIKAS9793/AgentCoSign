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
import logging
import sqlite3
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass, replace
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from functools import partial
from typing import Any

from agentcosign.clients.paypal import PayPalClient
from agentcosign.core import trace
from agentcosign.core.config import (
    ApprovalSettings,
    AuditSettings,
    DisplaySettings,
    PolicySettings,
    ReconciliationSettings,
    ScreeningSettings,
)
from agentcosign.core.exceptions import (
    AgentCoSignError,
    AuditIntegrityError,
    ConfigurationError,
    FailClosedError,
    PayPalClientError,
)
from agentcosign.models.domain import Decision, ToolAction
from agentcosign.models.policy import (
    OrderEvidence,
    Proposal,
    ReadingStatus,
    ScreeningVerdict,
    Signals,
)
from agentcosign.services import customer_copy, run_report
from agentcosign.services.ai_checks import CustomerChecks
from agentcosign.services.approval_view import (
    RECENT_DONE,
    SECTIONS,
    UNKNOWN_NAME,
    business_reasons,
    checked_time,
    customer_asked,
    decide_by,
    display_name,
    history_events,
    money_view,
    order_ending,
    order_facts,
    paypal_checked,
    placement,
    read_mark,
    refused_at_approval,
    safety_check,
    scan_note,
    short_flag,
    waiting_note,
)
from agentcosign.services.evidence import fetch_evidence
from agentcosign.services.executor import execute
from agentcosign.services.ledger import (
    CONTROL_REQUEST,
    NO_LINE_GIVEN,
    Ledger,
    ProposalState,
    Recorded,
)
from agentcosign.services.messages import MessageStore
from agentcosign.services.order_reads import OrderReads, Read
from agentcosign.services.passkeys import PasskeyRegistry
from agentcosign.services.policy_engine import VELOCITY_WINDOW
from agentcosign.services.reconciliation import Report, compare

# longer than one PayPal call can take, so a retry never overlaps a live attempt
RETRY_QUIET_SECONDS = 60
VIEW_REQUEST = "approver-session"
CONTROL_INTENTS = ("stop", "resume")
CONTROL_DIGEST = hashlib.sha256(b"stop or resume all refunds").hexdigest()
RULE_STOPPED = "R16"
SETTING_KEYS = (
    "auto_limit",
    "max_limit",
    "daily_cap",
    "repeat_limit",
    "decide_minutes",
    "window_hours",
)
VIEW_DIGEST = hashlib.sha256(b"view pending approvals").hexdigest()


logger = logging.getLogger(__name__)

# a request whose customer message was not checked: the safety scan counts as
# "could not run", so a person decides and is told exactly that
UNCHECKED = Signals(screening=ScreeningVerdict.UNAVAILABLE)


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
    # why an approved request could not go out after a fresh check, in owner words
    merchant_reason: str | None = None


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


PASSKEY_CONTROL = "control:passkeys"
WEBHOOK_HEADERS = (
    "paypal-auth-algo",
    "paypal-cert-url",
    "paypal-transmission-id",
    "paypal-transmission-sig",
    "paypal-transmission-time",
)
# events that can change what is left to refund or whether a refund finished
REFUND_EVENTS = frozenset(
    {"PAYMENT.CAPTURE.REFUNDED", "PAYMENT.CAPTURE.REVERSED", "PAYMENT.REFUND.FAILED"}
)
DISPUTE_EVENTS = frozenset(
    {
        "CUSTOMER.DISPUTE.CREATED",
        "CUSTOMER.DISPUTE.UPDATED",
        "CUSTOMER.DISPUTE.RESOLVED",
    }
)


def _reconciliation_view(
    last_run: Mapping[str, Any] | None, lookback_days: int
) -> dict[str, str]:
    """The last comparison with PayPal for the home screen; empty if never run."""
    if last_run is None:
        return {}
    return {
        "at": str(last_run["at"]),
        "breaks": str(last_run.get("breaks", 0)),
        "outside": str(last_run.get("outside", 0)),
        "unchecked": str(last_run.get("unchecked", 0)),
        "lookback_days": str(lookback_days),
    }


def revoke_approver(
    passkeys: PasskeyRegistry, ledger: Ledger, approver: str, now: datetime
) -> int:
    """Revoke an approver's passkeys and record it in the chain atomically.

    Sessions opened with those passkeys stop working on their next request.
    """
    if passkeys.path != ledger.path:
        raise FailClosedError("passkeys and ledger must share one database")

    def record(conn: sqlite3.Connection, count: int) -> None:
        """Write the revocation to the chain in the revoke's own transaction."""
        body = {"approver": approver, "count": count}
        ledger.append_event(conn, PASSKEY_CONTROL, "passkey_revoked", body, now)

    return passkeys.revoke(approver, now, record)


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
        approval: ApprovalSettings,
        reconciliation: ReconciliationSettings,
        audit: AuditSettings,
        display: DisplaySettings,
        webhook_id: str = "",
        clock: Callable[[], datetime] = _utc_now,
        messages: MessageStore | None = None,
        checks: CustomerChecks | None = None,
    ) -> None:
        self._ledger = ledger
        self._messages = messages
        self._checks = checks
        self._passkeys = passkeys
        self._paypal = paypal
        self._policy = policy
        self._screening = screening
        self._secret = secret
        self._open_seconds = approval.open_seconds
        self._session_seconds = approval.session_seconds
        self._enrolment_seconds = approval.enrolment_link_seconds
        self._reconciliation = reconciliation
        self._full_check_every = timedelta(hours=audit.full_check_hours)
        self._last_full_check: datetime | None = None
        # false once the scheduled full check finds a change to saved history
        self._records_ok = True
        # display reads follow this gateway's clock, including when tests move it
        self._reads = OrderReads(
            paypal,
            lambda: self._clock(),
            max_parallel=display.max_parallel_reads,
            reuse_seconds=display.reuse_seconds,
        )
        self._webhook_id = webhook_id
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
        signals = await self._checked_signals(proposal, now)
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
            if not recorded.replayed:
                await asyncio.to_thread(
                    self._ledger.record_greeting, proposal.request_id, name is not None
                )
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
        if await asyncio.to_thread(self._ledger.refunds_stopped):
            return released  # on-hold requests wait until refunds are back on
        for request_id in await asyncio.to_thread(self._ledger.releases_due, now):
            loaded = await asyncio.to_thread(self._ledger.load, request_id)
            if loaded is None:
                continue
            proposal = loaded[0]
            evidence = await self._evidence(proposal, now)
            if evidence is None:
                continue
            signals = await self._signals(request_id)
            try:
                recorded = await asyncio.to_thread(
                    self._ledger.release,
                    request_id,
                    evidence,
                    signals,
                    self._screening,
                    now,
                )
            except FailClosedError as exc:
                # declined, moved on or refunds stopped since the list was read:
                # the request keeps its state and the other releases continue
                logger.info("release skipped for %s: %s", request_id, exc.message)
                continue
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
            return datetime.fromisoformat(decide_by(changed, self._open_seconds))
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
        if state is ProposalState.EXPIRED:
            return customer_copy.expired(order)
        rule = await asyncio.to_thread(self._ledger.rule_of, request_id)
        if state is ProposalState.DENIED and rule == RULE_STOPPED:
            return customer_copy.stopped(order)
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
        digest = self._passkeys.register(approver, credential, self._clock())
        trace.emit("passkey", "Passkey registered (public key stored, WebAuthn)")
        return digest

    def session_options(self) -> str:
        """Passkey challenge for opening an approver session to view requests."""
        return self._passkeys.approval_options(
            VIEW_REQUEST, "view", VIEW_DIGEST, self._clock()
        )

    def open_session(self, credential: dict[str, Any]) -> tuple[str, str]:
        """Verify the approver's passkey; return a session token and their name."""
        now = self._clock()
        approver = self._passkeys.verify_approval(
            VIEW_REQUEST, "view", VIEW_DIGEST, credential, now
        )
        token = self._token(
            "approver-session", approver.credential_digest, self._session_seconds
        )
        trace.emit("passkey", "Passkey signature verified: session opened")
        return token, display_name(approver.name)

    def session_minutes(self) -> int:
        """How long a signed-in phone stays open, for the sign-out message."""
        return self._session_seconds // 60

    def account_ending(self) -> str:
        """The last four characters of the PayPal merchant account in use."""
        return self._ledger.account_id[-4:]

    def session_is_valid(self, token: str) -> bool:
        """True for an unexpired, untampered session whose passkey is still active."""
        if not self._token_is_valid("approver-session", token):
            return False
        return self._passkeys.is_active(token.split(".")[0])

    def enrolment_token(self) -> str:
        """A one-time setup link token for this gateway's own workspace."""
        return self._token("enrolment", "self-setup", self._enrolment_seconds)

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
        now = self._clock()
        waiting = await asyncio.to_thread(self._ledger.pending_approvals)
        paused = await asyncio.to_thread(self._ledger.paused, now)
        late = await asyncio.to_thread(self._ledger.late_failures, now)
        reads = await self._display_reads(
            item["order_id"] for item in [*waiting, *paused, *late]
        )
        views: list[dict[str, str | list[str]]] = []
        views += await self._waiting_views(waiting, reads)
        views += self._paused_views(paused, reads)
        views += await self._sending_views()
        views += self._late_failure_views(late, reads)
        return views

    async def _display_reads(self, order_ids: Iterable[str]) -> dict[str, Read]:
        """PayPal's facts for the screens: each order once, a few at a time, and
        reused briefly while none of our records changed. Never used to decide."""
        version = await asyncio.to_thread(self._ledger.version)
        return await self._reads.read_many(order_ids, version)

    def _late_failure_views(
        self, items: list[dict[str, str]], reads: dict[str, Read]
    ) -> list[dict[str, str | list[str]]]:
        views: list[dict[str, str | list[str]]] = []
        for item in items:
            read = reads[item["order_id"]]
            views.append(
                {
                    **self._basics(item, "failed_late"),
                    **read_mark(read.facts, read.at),
                    "customer": read.facts["customer"],
                    "failed_at": item["updated_at"],
                }
            )
        return views

    def _basics(self, item: dict[str, str], status: str) -> dict[str, str | list[str]]:
        return {
            "status": status,
            "request_id": item["request_id"],
            "tool": item["tool"],
            "amount": item["amount"],
            "currency": item["currency"],
            "order_id": item["order_id"],
            "order_ending": order_ending(item["order_id"]),
            # filled in only by cards whose facts were really read from PayPal
            "paypal_read": "",
            "checked_at": "",
        }

    async def _waiting_views(
        self, items: list[dict[str, str]], reads: dict[str, Read]
    ) -> list[dict[str, str | list[str]]]:
        views: list[dict[str, str | list[str]]] = []
        for item in items:
            read = reads[item["order_id"]]
            facts = read.facts
            triggered = json.loads(item["triggered"] or "[]")
            waiting = await asyncio.to_thread(
                self._ledger.reserved_on_order, item["order_id"], item["request_id"]
            )
            views.append(
                {
                    **self._basics(item, "waiting"),
                    **read_mark(facts, read.at),
                    "reasons": business_reasons(triggered, self._policy),
                    "safety_check": safety_check(triggered),
                    "scan_note": scan_note(triggered, self._policy),
                    "flag": short_flag(triggered, self._policy),
                    "customer_asked": await self._customer_asked(
                        item["request_id"], item["order_id"]
                    ),
                    **money_view(facts, item["amount"], waiting),
                    "asked_at": item["created_at"],
                    "decide_by": decide_by(item["updated_at"], self._open_seconds),
                    **await self._refund_split(item["order_id"], facts),
                    **facts,
                }
            )
        return views

    def _paused_views(
        self, items: list[dict[str, str]], reads: dict[str, Read]
    ) -> list[dict[str, str | list[str]]]:
        limit = self._policy.velocity_max_refunds_24h
        views: list[dict[str, str | list[str]]] = []
        for item in items:
            read = reads[item["order_id"]]
            facts = read.facts
            many = int(item["requests_24h"]) >= 2 * limit
            views.append(
                {
                    **self._basics(item, "paused"),
                    **read_mark(facts, read.at),
                    "customer": facts["customer"],
                    "release_at": item["release_at"],
                    # this request would be the next refund after those counted
                    "count_in_window": str(int(item["refunds_24h"]) + 1),
                    "asked_at": item["created_at"],
                    **self._paused_line(item, facts),
                    "many_requests": "yes" if many else "no",
                    "rechecking": "yes"
                    if item["release_at"] <= self._clock().isoformat()
                    else "no",
                }
            )
        return views

    def _paused_line(
        self, item: dict[str, str], facts: dict[str, str]
    ) -> dict[str, str]:
        """The line the customer was given when the request was paused.

        Only whether it greeted them by name was kept. A greeting without a name
        is shown as given; a name PayPal cannot give now is left out and the line
        is marked as not exact, rather than shown as if it were what they saw.
        """
        named = item.get("greeting_named", "")
        if named == NO_LINE_GIVEN:
            return {"customer_message": "", "customer_message_exact": NO_LINE_GIVEN}
        live = customer_copy.first_name(facts["customer"], UNKNOWN_NAME)
        name = live if named != "no" else None
        exact = named == "no" or (named == "yes" and live is not None)
        release = datetime.fromisoformat(item["release_at"])
        asked = datetime.fromisoformat(item["created_at"])
        hours = customer_copy.hours_until(release, asked)
        return {
            "customer_message": customer_copy.paused(
                name, item["order_id"], item["amount"], hours
            ),
            "customer_message_exact": "yes" if exact else "no",
        }

    async def _refund_split(
        self, order_id: str, facts: dict[str, str]
    ) -> dict[str, str]:
        """Refunds sent through this app versus refunded in PayPal directly."""
        if not facts.get("paid"):
            return {"refunded_in_app": "", "refunded_outside": ""}
        in_app = await asyncio.to_thread(self._ledger.refunded_through_app, order_id)
        outside = max(Decimal(facts["already_refunded"]) - in_app, Decimal("0"))
        return {
            "refunded_in_app": f"{in_app:.2f}",
            "refunded_outside": f"{outside:.2f}",
        }

    async def summary(self) -> dict[str, str]:
        """The merchant's day and their settings, for the home screen."""
        day = await asyncio.to_thread(self._ledger.day_summary, self._clock())
        policy = self._policy
        return {
            **day,
            "account_ending": self.account_ending(),
            "auto_limit": f"{policy.refund_auto_limit:.2f}",
            "max_limit": f"{policy.refund_max_limit:.2f}",
            "daily_cap": f"{policy.daily_outflow_cap:.2f}",
            "repeat_limit": str(policy.velocity_max_refunds_24h),
            "decide_minutes": str(self._open_seconds // 60),
            "window_hours": str(int(VELOCITY_WINDOW.total_seconds() // 3600)),
        }

    async def done_today(
        self, limit: int | None = None
    ) -> list[dict[str, str | list[str]]]:
        """Requests that ended in the last 24 hours, newest first, for the Done
        section. Names are read from PayPal only for the requests returned."""
        items = await asyncio.to_thread(self._ledger.recent_outcomes, self._clock())
        items = items[:limit]
        reads = await self._display_reads(item["order_id"] for item in items)
        views: list[dict[str, str | list[str]]] = []
        for item in items:
            order = item["order_id"]
            views.append(
                {
                    **self._basics(item, "done"),
                    "outcome": item["state"],
                    # sent means PayPal confirmed it; anything else is still processing
                    "paypal_status": item.get("paypal_status", ""),
                    "customer": reads[order].facts["customer"],
                    "ended_at": item["updated_at"],
                }
            )
        return views

    async def done_page(self) -> dict[str, Any]:
        """The latest ended requests and how many ended in all, so a capped list
        is never presented as the whole day."""
        items = await self.done_today()
        total = await asyncio.to_thread(self._ledger.recent_count, self._clock())
        return {"items": items, "total": str(total)}

    async def history(self, request_id: str) -> dict[str, Any] | None:
        """One request's record in owner words, and whether the record checks out."""
        rows = await asyncio.to_thread(self._ledger.events, request_id)
        if not rows:
            return None
        try:
            await asyncio.to_thread(self._ledger.verify_request, request_id)
            sealed = self._records_ok
        except AuditIntegrityError:
            sealed = False
        first = rows[0]
        return {
            "request_id": request_id,
            "amount": str(first.get("amount", "")),
            "order_ending": order_ending(first.get("order_id")),
            "events": history_events(rows),
            "sealed": sealed,
        }

    async def handle_webhook(
        self, headers: Mapping[str, str], event: Mapping[str, Any]
    ) -> str:
        """Verify a PayPal event with PayPal, record it once, then re-read PayPal.

        The event body is never trusted for money facts: it only prompts fresh
        reads, so a forged or stale event cannot change a decision.

        Raises:
            ConfigurationError: when no webhook id is configured.
            FailClosedError: when headers are missing or PayPal does not verify it.
            PayPalClientError: when PayPal cannot be reached to verify it.
        """
        if not self._webhook_id:
            raise ConfigurationError("PayPal webhooks are not configured")
        missing = [name for name in WEBHOOK_HEADERS if not headers.get(name)]
        event_id, event_type = event.get("id"), event.get("event_type")
        if missing or not isinstance(event_id, str) or not isinstance(event_type, str):
            raise FailClosedError("the webhook is missing its signature or identity")
        if not await self._paypal.verify_webhook(
            headers, dict(event), self._webhook_id
        ):
            raise FailClosedError("PayPal did not verify this webhook")
        if event_type not in REFUND_EVENTS | DISPUTE_EVENTS:
            return "ignored"
        resource = event.get("resource")
        resource_id = (
            str(resource.get("id", "")) if isinstance(resource, Mapping) else ""
        )
        recorded = await asyncio.to_thread(
            self._ledger.record_webhook,
            event_id,
            event_type,
            resource_id,
            self._clock(),
        )
        if not recorded:
            return "duplicate"
        if event_type in REFUND_EVENTS:
            await self.settle_pending()
        return "accepted"

    async def reconcile(self) -> Report:
        """Compare every refund we sent with PayPal's records and record the run."""
        now = self._clock()
        since = now - timedelta(days=self._reconciliation.lookback_days)
        sent, order_ids, known = await asyncio.to_thread(
            self._ledger.reconciliation_inputs, since
        )
        orders: dict[str, Mapping[str, Any] | None] = {}
        for order_id in order_ids:
            try:
                orders[order_id] = await self._paypal.get_order(order_id)
            except PayPalClientError:
                orders[order_id] = None  # reported as unchecked, never as clean
        stale = timedelta(hours=self._reconciliation.stale_processing_hours)
        report = compare(sent, orders, now, stale, since=since, known=known)
        body: dict[str, Any] = {
            **report.summary(),
            "break_list": [
                {"request_id": b.request_id, "kind": b.kind} for b in report.breaks
            ],
        }
        await asyncio.to_thread(self._ledger.record_reconciliation, body, now)
        if report.breaks:
            logger.warning("reconciliation found %d breaks", len(report.breaks))
        return report

    async def reconcile_if_due(self) -> Report | None:
        """Run reconciliation when the last run is older than the interval."""
        last = await asyncio.to_thread(self._ledger.last_reconciliation)
        interval = timedelta(hours=self._reconciliation.interval_hours)
        if (
            last is not None
            and self._clock() - datetime.fromisoformat(str(last["at"])) < interval
        ):
            return None
        return await self.reconcile()

    async def check_records_if_due(self) -> bool | None:
        """Check the whole saved record at most once per interval.

        Returns None when not due, otherwise whether it checked out. A change
        found here marks every request's history until the record checks out.
        """
        now = self._clock()
        last = self._last_full_check
        if last is not None and now - last < self._full_check_every:
            return None
        self._last_full_check = now
        try:
            await asyncio.to_thread(self._ledger.verify_records)
        except AuditIntegrityError:
            logger.error("the full record check found a change to saved history")
            self._records_ok = False
            return False
        self._records_ok = True
        return True

    async def chain_head(self) -> dict[str, Any]:
        """The latest chain entry, safe to publish: no amounts, names or ids."""
        return await asyncio.to_thread(self._ledger.head)

    def live_version(self) -> int:
        """Grows with every recorded change; the phone re-reads when it moves."""
        return self._ledger.version()

    async def home(self, session_token: str) -> dict[str, Any]:
        """Everything the merchant's home shows, decided here, not on the phone.

        Each request carries its section and whether it can be sent; the phone
        only formats. The check time is given only when PayPal was really read.
        """
        views = await self.pending()
        day = await self.summary()
        done = await self.done_today(limit=RECENT_DONE)
        now = self._clock()
        stopped = await asyncio.to_thread(self._ledger.refunds_stopped)
        last_run = await asyncio.to_thread(self._ledger.last_reconciliation)
        done_total = await asyncio.to_thread(self._ledger.recent_count, now)
        sections: dict[str, list[dict[str, Any]]] = {name: [] for name in SECTIONS}
        for view in views:
            place = placement(view, now, stopped=stopped)
            sections[place["section"]].append(
                {**view, **place, "waiting_note": waiting_note(view)}
            )
        checked = paypal_checked(views)
        subject = session_token.split(".")[0]
        name = await asyncio.to_thread(self._passkeys.name_for, subject)
        decidable = [str(i["decide_by"]) for i in sections["needs_decision"]]
        return {
            "server_now": now.isoformat(),
            "refunds_stopped": "yes" if stopped else "no",
            "reconciliation": _reconciliation_view(
                last_run, self._reconciliation.lookback_days
            ),
            "paypal_checked": checked,
            "checked_at": checked_time(views, checked),
            "approver_display": display_name(name),
            "account_ending": self.account_ending(),
            "counts": {key: str(len(items)) for key, items in sections.items()},
            "soonest_decide_by": min(decidable, default=""),
            "sections": sections,
            "done": {"recent": done, "total": str(done_total)},
            "today": {k: v for k, v in day.items() if k not in SETTING_KEYS},
            "settings": {k: day[k] for k in SETTING_KEYS},
        }

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
        return self._ledger.expire_due(self._clock(), self._open_seconds)

    # ------------------------------------------------------------- approvals

    def _pending_digest(self, request_id: str, intent: str) -> str:
        state = self._ledger.state_of(request_id)
        declining_a_pause = state is ProposalState.PAUSED and intent == "reject"
        if state is not ProposalState.PENDING_APPROVAL and not declining_a_pause:
            raise FailClosedError("this request is not waiting for approval")
        if intent == "approve" and self._ledger.refunds_stopped():
            raise FailClosedError("refunds are stopped, so nothing can be approved")
        if intent == "approve" and self._past_deadline(request_id):
            raise FailClosedError("the time to decide this request has passed")
        digest = self._ledger.action_digest(request_id)
        if digest is None:
            raise FailClosedError("unknown request id")
        return digest

    def _past_deadline(self, request_id: str) -> bool:
        """True once the time to decide has run out, before any fingerprint."""
        timing = self._ledger.timing(request_id)
        if timing is None:
            return True
        deadline = datetime.fromisoformat(decide_by(timing[2], self._open_seconds))
        return self._clock() >= deadline

    async def approval_options(
        self,
        request_id: str,
        intent: str,
        seen: tuple[Decimal, Decimal] | None = None,
    ) -> str:
        """Passkey challenge bound to this exact pending action and intent.

        An approval is only offered on the figures PayPal shows now: ``seen`` is
        what the owner's screen says was paid and already refunded.
        """
        digest = await asyncio.to_thread(self._pending_digest, request_id, intent)
        if intent == "approve":
            await self._confirm_shown(request_id, seen)
        return await asyncio.to_thread(
            self._passkeys.approval_options, request_id, intent, digest, self._clock()
        )

    async def _confirm_shown(
        self, request_id: str, seen: tuple[Decimal, Decimal] | None
    ) -> None:
        """Check the owner's figures against PayPal now and keep them as approved."""
        loaded = await asyncio.to_thread(self._ledger.load, request_id)
        if loaded is None:
            raise FailClosedError("unknown request id")
        proposal = loaded[0]
        if not proposal.order_id:
            return  # payouts carry no order figures
        if seen is None:
            raise FailClosedError("the figures on screen are needed to approve")
        now = self._clock()
        evidence = await self._evidence(proposal, now)
        if evidence is None:
            raise FailClosedError("PayPal could not be reached, so nothing was sent")
        if (evidence.captured_amount, evidence.refunded_amount) != seen:
            self._reads.forget(proposal.order_id)  # the next screen reads PayPal
            raise FailClosedError("the figures changed since they were shown")
        await asyncio.to_thread(self._ledger.record_shown, request_id, evidence, now)

    def control_options(self, intent: str) -> str:
        """Passkey challenge to stop or resume all refunds."""
        if intent not in CONTROL_INTENTS:
            raise FailClosedError("unknown control")
        return self._passkeys.approval_options(
            CONTROL_REQUEST, intent, CONTROL_DIGEST, self._clock()
        )

    async def set_control(
        self, intent: str, credential: dict[str, Any]
    ) -> dict[str, str]:
        """Stop or resume all refunds with a fingerprint signed for that intent.

        A refund already with PayPal cannot be recalled; the answer says how
        many are still finishing so the merchant is not told otherwise.
        """
        if intent not in CONTROL_INTENTS:
            raise FailClosedError("unknown control")
        now = self._clock()
        approver = self._passkeys.verify_approval(
            CONTROL_REQUEST, intent, CONTROL_DIGEST, credential, now
        )
        changed = await asyncio.to_thread(
            self._ledger.set_refunds_stopped, intent == "stop", approver.name, now
        )
        sending = await asyncio.to_thread(self._ledger.sending)
        stopped = await asyncio.to_thread(self._ledger.refunds_stopped)
        return {
            "refunds_stopped": "yes" if stopped else "no",
            "changed": "yes" if changed else "no",
            "still_sending": str(len(sending)),
        }

    async def decide_approval(
        self, request_id: str, intent: str, credential: dict[str, Any]
    ) -> Outcome:
        """Apply a passkey-signed approve or reject, then execute if approved."""
        begun = run_report.silent_begin()
        outcome = await self._decide_signed(request_id, intent, credential)
        await self._report_approval(begun, request_id, outcome)
        return outcome

    async def _report_approval(
        self, begun: run_report.Begun, request_id: str, outcome: Outcome
    ) -> None:
        """The closing step of an approval or decline, for the live step record."""
        if not trace.active():
            return
        loaded = await asyncio.to_thread(self._ledger.load, request_id)
        proposal = None if loaded is None else loaded[0]
        ending = run_report.Ending(
            outcome.decision,
            outcome.state,
            outcome.triggered,
            outcome.paypal_status,
            None if proposal is None else proposal.amount,
            "USD" if proposal is None else proposal.currency,
        )
        run_report.finish(begun, ending, run_report.AFTER_APPROVAL)

    async def _decide_signed(
        self, request_id: str, intent: str, credential: dict[str, Any]
    ) -> Outcome:
        now = self._clock()
        digest = self._pending_digest(request_id, intent)
        approver = self._passkeys.verify_approval(
            request_id, intent, digest, credential, now
        )
        trace.emit(
            "passkey",
            f"Passkey signature verified for {intent}",
            request=request_id,
            signed_digest=digest[:12],
        )
        detail = {
            "assurance": "passkey",
            "approver": approver.name,
            "passkey": approver.credential_digest,
        }
        if intent == "approve":
            return await self._approve(request_id, approver.name, detail, now)
        await asyncio.to_thread(
            partial(self._ledger.transition, decide_within=self._open_seconds),
            request_id,
            ProposalState.DENIED,
            now,
            detail,
        )
        # a decline can bring the customer back under the limit: re-decide
        # their pauses now rather than on the next background pass
        await self.release_due()
        return Outcome(
            request_id=request_id,
            decision="rejected",
            rule_id="",
            explanation=f"Rejected by {approver.name} with a passkey.",
            state=ProposalState.DENIED.value,
            triggered=(),
        )

    async def _approve(
        self, request_id: str, name: str, detail: dict[str, str], now: datetime
    ) -> Outcome:
        """Decide the request again from PayPal's current record, then send it if
        the rules still allow it or still ask a person (the approval answers that).
        """
        loaded = await asyncio.to_thread(self._ledger.load, request_id)
        if loaded is None:
            raise FailClosedError("unknown request id")
        proposal = loaded[0]
        evidence = await self._evidence(proposal, now)
        if proposal.order_id and evidence is None:
            await self._not_applied(request_id, "paypal_unreadable", detail, now)
            raise FailClosedError("PayPal could not be reached, so nothing was sent")
        recorded = await self._apply_approval(request_id, evidence, detail)
        state = recorded.state or ProposalState.DENIED
        base = Outcome(
            request_id=request_id,
            decision="approved",
            rule_id="",
            explanation=f"Approved by {name} with a passkey.",
            state=state.value,
            triggered=(),
        )
        if state is ProposalState.EXECUTING:
            return (
                base if evidence is None else await self._run(proposal, evidence, base)
            )
        result = recorded.result
        if state in (ProposalState.DENIED, ProposalState.PAUSED):
            await self.release_due()
        return replace(
            base,
            rule_id=result.rule_id,
            triggered=result.triggered_rules,
            merchant_reason=refused_at_approval(result.rule_id, self._policy),
        )

    async def _apply_approval(
        self,
        request_id: str,
        evidence: OrderEvidence | None,
        detail: dict[str, str],
    ) -> Recorded:
        """Write the approval; one that cannot be applied stays on the record."""
        # the deadline is judged when the approval is written, after the PayPal read
        signals = await self._signals(request_id)
        now = self._clock()
        try:
            return await asyncio.to_thread(
                partial(self._ledger.approve, decide_within=self._open_seconds),
                request_id,
                evidence,
                signals,
                self._screening,
                now,
                detail,
            )
        except FailClosedError as refused:
            await self._not_applied(request_id, refused.message, detail, now)
            raise

    async def _not_applied(
        self, request_id: str, reason: str, detail: dict[str, str], now: datetime
    ) -> None:
        """Keep a signed approval that moved nothing on the record, with why.

        If the note itself cannot be written, that is logged and the caller still
        reports the original refusal.
        """
        try:
            await asyncio.to_thread(
                self._ledger.record_unapplied_approval, request_id, reason, detail, now
            )
        except (sqlite3.Error, AgentCoSignError):
            logger.exception("could not record an approval that moved nothing")

    async def _signals(self, request_id: str) -> Signals:
        """The checks a request was first decided on; a request without them is
        treated as unchecked, which asks the merchant."""
        if self._messages is not None:
            stored = await asyncio.to_thread(self._messages.checks_for, request_id)
            if stored is not None:
                return stored.signals
        return UNCHECKED

    async def keep_message(self, text: str, now: datetime) -> str:
        """Mask and keep a customer's message; returns its id."""
        if self._messages is None:
            raise FailClosedError("customer messages are not kept here")
        return await asyncio.to_thread(self._messages.add, text, now)

    async def message_text(self, message_id: str) -> str | None:
        """A kept message as stored (masked), or None if unknown."""
        if self._messages is None:
            return None
        return await asyncio.to_thread(self._messages.text, message_id)

    async def _customer_asked(self, request_id: str, order_id: str = "") -> str:
        """The card's sentence on what the customer's message asked for."""
        if self._messages is None:
            return ""
        stored = await asyncio.to_thread(self._messages.checks_for, request_id)
        if stored is None or "checks" in stored.record:
            return ""
        signals = stored.signals
        read = signals.reading_status is not ReadingStatus.UNAVAILABLE
        return customer_asked(signals.reading, read, order_id or None)

    async def _checked_signals(self, proposal: Proposal, now: datetime) -> Signals:
        """Run the AI checks on the customer's message the proposal answers.

        A repeat of the same request reuses the first result. A proposal with no
        message, an unknown message or checks switched off is unchecked.
        """
        if proposal.message_id is None or self._messages is None:
            return await self._signals(proposal.request_id)
        stored = await asyncio.to_thread(self._messages.checks_for, proposal.request_id)
        if stored is not None:
            return stored.signals
        text = await asyncio.to_thread(self._messages.text, proposal.message_id)
        if text is None or self._checks is None:
            signals: Signals = UNCHECKED
            record = {"checks": "message unknown" if text is None else "checks off"}
        else:
            signals, record = await self._checks.run(text)
        await asyncio.to_thread(
            self._messages.save_checks,
            proposal.request_id,
            proposal.message_id,
            signals,
            record,
            now,
        )
        return signals
