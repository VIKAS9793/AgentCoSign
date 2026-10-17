"""The ledger: decisions, reservations and a tamper-evident audit trail.

Three guarantees live here, not in the policy engine:

* **Atomic reservation.** Reading the counters, deciding and recording the
  outcome happen in one write transaction, so two concurrent requests cannot
  both spend the same remaining refund or the same daily budget.
* **Idempotency.** The request id is the unique key. Repeating a request returns
  the original outcome and never decides twice; reusing an id with different
  content is refused.
* **Tamper evidence.** Every event is appended to a hash chain, and each link is
  authenticated with a key that is not stored in the database, so editing a row
  or rewriting the whole chain without the key is detected.

Refunds already executed are counted by PayPal's own record (the evidence), so
the ledger only adds what is reserved but not yet executed for an order.
"""

import hashlib
import hmac
import json
import sqlite3
import threading
from collections.abc import Iterable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import Decimal, InvalidOperation
from enum import StrEnum
from pathlib import Path
from typing import Any

from agentcosign.core.config import PolicySettings, ScreeningSettings
from agentcosign.core.exceptions import AuditIntegrityError, FailClosedError
from agentcosign.models.domain import Decision, PolicyResult, ToolAction
from agentcosign.models.policy import (
    REQUEST_ID_PATTERN,
    LedgerContext,
    OrderEvidence,
    Proposal,
    Signals,
)
from agentcosign.services.identity import payee_key
from agentcosign.services.policy_engine import (
    VELOCITY_WINDOW,
    check_execution_drift,
    evaluate,
)
from agentcosign.services.reconciliation import Known, OurRefund

GENESIS_HASH = "0" * 64
# the emergency stop is recorded in the chain under this id, so it is provable
CONTROL_REQUEST = "control:refunds"
RECONCILE_REQUEST = "control:reconciliation"
WEBHOOK_REQUEST = "control:webhooks"
STOP_CONTROL = "refunds_stopped"
STOP_EVENTS = {"refunds_stopped": "yes", "refunds_resumed": "no"}
BUSY_TIMEOUT_SECONDS = 5
CENT = Decimal("0.01")


class ProposalState(StrEnum):
    """Lifecycle of a proposal after the decision."""

    PENDING_APPROVAL = "pending_approval"
    PAUSED = "paused"
    EXECUTING = "executing"
    EXECUTED = "executed"
    DENIED = "denied"
    EXPIRED = "expired"
    FAILED = "failed"


S = ProposalState
# requests whose refund was sent or is being sent to PayPal
_SENT_STATES = frozenset({S.EXECUTING.value, S.EXECUTED.value})
# A paused refund keeps its money set aside so nothing else can promise it, but
# it is not yet one of the customer's refunds: paused requests never hold each
# other back, and each is counted once it is released.
RESERVING = (S.PENDING_APPROVAL, S.EXECUTING, S.PAUSED)
OUTFLOW = (S.PENDING_APPROVAL, S.EXECUTING, S.EXECUTED, S.PAUSED)
CUSTOMER_REFUNDS = (S.PENDING_APPROVAL, S.EXECUTING, S.EXECUTED)
ALLOWED_TRANSITIONS: dict[ProposalState, frozenset[ProposalState]] = {
    S.PENDING_APPROVAL: frozenset({S.EXECUTING, S.DENIED, S.EXPIRED}),
    # Declining is always possible; sending only happens through a release.
    S.PAUSED: frozenset({S.DENIED}),
    # Back to approval when the order's state moved between decision and execution.
    S.EXECUTING: frozenset({S.EXECUTED, S.FAILED, S.PENDING_APPROVAL}),
    # PayPal can report a pending refund as failed or cancelled later.
    S.EXECUTED: frozenset({S.FAILED}),
}
COMPLETED_STATUS = "COMPLETED"
UNSETTLED_STATUSES = ("PENDING",)
LATE_FAILURE_STATUSES = ("FAILED", "CANCELLED")
STATE_FOR_DECISION = {
    Decision.ALLOW: S.EXECUTING,
    Decision.ESCALATE: S.PENDING_APPROVAL,
    Decision.PAUSE: S.PAUSED,
    Decision.DENY: S.DENIED,
}

_SCHEMA = """
CREATE TABLE IF NOT EXISTS proposals (
    request_id TEXT PRIMARY KEY,
    payload_hash TEXT NOT NULL,
    tool TEXT NOT NULL,
    order_id TEXT,
    customer_key TEXT,
    payee_key TEXT,
    reference TEXT,
    amount TEXT NOT NULL,
    currency TEXT NOT NULL,
    state TEXT NOT NULL,
    decision TEXT NOT NULL,
    rule_id TEXT NOT NULL,
    explanation TEXT NOT NULL,
    triggered TEXT NOT NULL,
    attempts INTEGER NOT NULL DEFAULT 0,
    proposal_json TEXT,
    evidence_json TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_proposals_order ON proposals (order_id, state);
CREATE INDEX IF NOT EXISTS ix_proposals_created ON proposals (created_at);
CREATE INDEX IF NOT EXISTS ix_proposals_customer ON proposals (customer_key);
CREATE INDEX IF NOT EXISTS ix_proposals_payee ON proposals (payee_key);
CREATE TABLE IF NOT EXISTS audit (
    seq INTEGER PRIMARY KEY AUTOINCREMENT,
    created_at TEXT NOT NULL,
    request_id TEXT NOT NULL,
    event TEXT NOT NULL,
    body TEXT NOT NULL,
    prev_hash TEXT NOT NULL,
    entry_hash TEXT NOT NULL,
    mac TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS webhook_events (
    event_id TEXT PRIMARY KEY,
    event_type TEXT NOT NULL,
    received_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS controls (
    name TEXT PRIMARY KEY,
    value TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
"""

_Q_GET = "SELECT * FROM proposals WHERE request_id = ?"
_Q_ORDER_RESERVED = (
    "SELECT amount FROM proposals WHERE order_id = ? AND tool IN (?, ?) "
    "AND state IN (?, ?, ?) AND request_id != ?"
)
_Q_OUTFLOW = (
    "SELECT amount FROM proposals WHERE decided_at >= ? AND state IN (?, ?, ?, ?) "
    "AND request_id != ?"
)
_Q_CUSTOMER = (
    "SELECT decided_at FROM proposals WHERE customer_key = ? AND tool = ? "
    "AND decided_at >= ? AND state IN (?, ?, ?) AND request_id != ?"
)
# Refunds that went out (or are going out) without a person deciding.
_Q_AUTO_ORDER = (
    "SELECT amount FROM proposals WHERE order_id = ? AND tool = ? AND decision = ? "
    "AND state IN (?, ?) AND request_id != ?"
)
_Q_AUTO_CUSTOMER = (
    "SELECT amount FROM proposals WHERE customer_key = ? AND tool = ? "
    "AND decision = ? AND decided_at >= ? AND state IN (?, ?) AND request_id != ?"
)
_Q_CUSTOMER_REFUND_COUNT = (
    "SELECT COUNT(*) FROM proposals WHERE customer_key = ? AND tool = ? "
    "AND decided_at >= ? AND state IN (?, ?, ?)"
)
_Q_CUSTOMER_ASKS = (
    "SELECT COUNT(*) FROM proposals WHERE customer_key = ? AND tool = ? "
    "AND created_at >= ?"
)
_Q_STAMP = "UPDATE proposals SET decided_at = ?, release_at = ? WHERE request_id = ?"
# A counted refund dropping out (declined, expired, failed) can bring the
# customer back under the limit, so their pauses are re-checked on the next pass.
_Q_RECHECK_PAUSES = (
    "UPDATE proposals SET release_at = ? WHERE state = ? AND customer_key = ? "
    "AND release_at > ?"
)
DROPPED_OUT = (S.DENIED, S.EXPIRED, S.FAILED)
_Q_REDECIDE = (
    "UPDATE proposals SET state = ?, decision = ?, rule_id = ?, explanation = ?, "
    "triggered = ?, evidence_json = ?, decided_at = ?, release_at = ?, attempts = 0, "
    "updated_at = ? WHERE request_id = ?"
)
_Q_RELEASE_DUE = (
    "SELECT request_id FROM proposals WHERE state = ? AND release_at <= ? "
    "ORDER BY release_at, created_at"
)
_Q_PAUSED = (
    "SELECT request_id, tool, amount, currency, order_id, customer_key, release_at, "
    "created_at, greeting_named FROM proposals WHERE state = ? ORDER BY release_at"
)
_Q_SAME_PAUSED = (
    "SELECT request_id FROM proposals WHERE state = ? AND order_id = ? AND tool = ? "
    "AND amount = ? ORDER BY created_at LIMIT 1"
)
_Q_KNOWN_PAYEE = "SELECT 1 FROM proposals WHERE payee_key = ? AND state = ? LIMIT 1"
_Q_DUPLICATE = (
    "SELECT 1 FROM proposals WHERE payee_key = ? AND amount = ? AND reference = ? "
    "AND created_at >= ? AND state IN (?, ?, ?, ?) LIMIT 1"
)
_Q_INSERT = (
    "INSERT INTO proposals (request_id, payload_hash, tool, order_id, customer_key, "
    "payee_key, reference, amount, currency, state, decision, rule_id, explanation, "
    "triggered, proposal_json, evidence_json, created_at, updated_at) "
    "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)"
)
_Q_SET_STATE = "UPDATE proposals SET state = ?, updated_at = ? WHERE request_id = ?"
_Q_REBASE = (
    "UPDATE proposals SET state = ?, evidence_json = ?, attempts = 0, "
    "updated_at = ? WHERE request_id = ?"
)
# the approval window runs from when the request reached the person, which
# for a released pause is the release, not the original request
_Q_DUE = (
    "SELECT request_id, customer_key FROM proposals WHERE state = ? AND updated_at < ?"
)
_Q_STALE = (
    "SELECT request_id FROM proposals WHERE state = ? AND updated_at < ? "
    "ORDER BY updated_at"
)
_Q_SENDING = (
    "SELECT request_id, tool, amount, currency, order_id, updated_at "
    "FROM proposals WHERE state = ? ORDER BY updated_at"
)
_Q_ATTEMPT = (
    "UPDATE proposals SET attempts = attempts + 1, updated_at = ? WHERE request_id = ?"
)
_Q_COLUMNS = "PRAGMA table_info(proposals)"
_MIGRATIONS = {
    "attempts": "ALTER TABLE proposals ADD COLUMN attempts INTEGER NOT NULL DEFAULT 0",
    "proposal_json": "ALTER TABLE proposals ADD COLUMN proposal_json TEXT",
    "evidence_json": "ALTER TABLE proposals ADD COLUMN evidence_json TEXT",
    "decided_at": "ALTER TABLE proposals ADD COLUMN decided_at TEXT",
    "release_at": "ALTER TABLE proposals ADD COLUMN release_at TEXT",
    "paypal_id": "ALTER TABLE proposals ADD COLUMN paypal_id TEXT",
    "paypal_status": "ALTER TABLE proposals ADD COLUMN paypal_status TEXT",
    # whether the line given at a pause greeted the customer by name (yes or no);
    # the name itself is never stored
    "greeting_named": "ALTER TABLE proposals ADD COLUMN greeting_named TEXT",
}
_Q_GREETING = "UPDATE proposals SET greeting_named = ? WHERE request_id = ?"
_Q_PAYPAL = (
    "UPDATE proposals SET paypal_id = ?, paypal_status = ?, updated_at = ? "
    "WHERE request_id = ?"
)
_Q_UNSETTLED = (
    "SELECT request_id FROM proposals WHERE state = ? AND paypal_status = ? "
    "ORDER BY updated_at"
)
_Q_VERSION = "SELECT COALESCE(MAX(seq), 0) FROM audit"
_Q_WAITING = "SELECT amount FROM proposals WHERE decided_at >= ? AND state IN (?, ?, ?)"
_Q_EVENTS = (
    "SELECT created_at, event, body FROM audit WHERE request_id = ? ORDER BY seq"
)
_Q_APP_REFUNDED = (
    "SELECT amount FROM proposals WHERE order_id = ? AND tool = ? AND state = ?"
)
_Q_RECENT = (
    "SELECT request_id, tool, amount, currency, order_id, state, updated_at, "
    "paypal_status FROM proposals WHERE state IN (?, ?, ?, ?) AND updated_at >= ? "
    "ORDER BY updated_at DESC LIMIT ?"
)
RECENT_LIMIT = 20
_Q_RECENT_COUNT = (
    "SELECT COUNT(*) FROM proposals WHERE state IN (?, ?, ?, ?) AND updated_at >= ?"
)
_Q_DAY = (
    "SELECT amount, decision, state, paypal_status FROM proposals "
    "WHERE tool = ? AND updated_at >= ?"
)
_Q_LATE_FAILURES = (
    "SELECT request_id, tool, amount, currency, order_id, updated_at FROM proposals "
    "WHERE state = ? AND paypal_status IN (?, ?) AND updated_at >= ? "
    "ORDER BY updated_at"
)
_Q_BACKFILL = "UPDATE proposals SET decided_at = created_at WHERE decided_at IS NULL"
_Q_PENDING = (
    "SELECT request_id, tool, amount, currency, order_id, rule_id, explanation, "
    "triggered, created_at, updated_at FROM proposals WHERE state = ? "
    "ORDER BY created_at"
)
_Q_ALL_PROPOSALS = "SELECT request_id, tool, amount, decision, state FROM proposals"
_Q_ONE_PROPOSAL = (
    "SELECT request_id, tool, amount, decision, state FROM proposals "
    "WHERE request_id = ?"
)
_Q_PREV_HASH = "SELECT entry_hash FROM audit WHERE seq < ? ORDER BY seq DESC LIMIT 1"
_Q_LAST_HASH = "SELECT entry_hash FROM audit ORDER BY seq DESC LIMIT 1"
_Q_HEAD = "SELECT seq, entry_hash, created_at FROM audit ORDER BY seq DESC LIMIT 1"
_Q_AT_SEQ = "SELECT entry_hash FROM audit WHERE seq = ?"
_Q_CONTROL_GET = "SELECT value FROM controls WHERE name = ?"
_Q_RULE = "SELECT rule_id FROM proposals WHERE request_id = ?"
_Q_WEBHOOK_SEEN = (
    "INSERT OR IGNORE INTO webhook_events (event_id, event_type, received_at) "
    "VALUES (?, ?, ?)"
)
_Q_RECONCILE_OURS = (
    "SELECT request_id, order_id, amount, paypal_id, updated_at FROM proposals "
    "WHERE tool = ? AND state = ? AND updated_at >= ?"
)
# an order is checked when any request on it was asked or changed in the window,
# so a request asked earlier but sent inside the window is never skipped
_Q_RECONCILE_ORDERS = (
    "SELECT DISTINCT order_id FROM proposals "
    "WHERE order_id IS NOT NULL AND order_id != '' "
    "AND (created_at >= ? OR updated_at >= ?)"
)
_Q_RECONCILE_KNOWN = (
    "SELECT request_id, state, paypal_id FROM proposals "
    "WHERE tool = ? AND order_id IN (SELECT value FROM json_each(?))"
)
_Q_CONTROL_SET = (
    "INSERT INTO controls (name, value, updated_at) VALUES (?, ?, ?) "
    "ON CONFLICT(name) DO UPDATE SET value = excluded.value, "
    "updated_at = excluded.updated_at"
)
_Q_APPEND = (
    "INSERT INTO audit (created_at, request_id, event, body, prev_hash, entry_hash, "
    "mac) VALUES (?, ?, ?, ?, ?, ?, ?)"
)
_CHAIN_COLUMNS = (
    "SELECT seq, created_at, request_id, event, body, prev_hash, entry_hash, mac "
    "FROM audit "
)
_Q_CHAIN = _CHAIN_COLUMNS + "ORDER BY seq"
_Q_CHAIN_AFTER = _CHAIN_COLUMNS + "WHERE seq > ? ORDER BY seq"
_Q_CHAIN_AT = _CHAIN_COLUMNS + "WHERE seq = ?"
_Q_REQUEST_CHAIN = _CHAIN_COLUMNS + "WHERE request_id = ? ORDER BY seq"


@dataclass(frozen=True, slots=True)
class Recorded:
    """Outcome of submitting a proposal to the ledger."""

    request_id: str
    state: ProposalState | None  # None when nothing was stored
    result: PolicyResult
    replayed: bool = False


def canonical_json(value: Any) -> str:
    """Deterministic JSON used for hashing (sorted keys, no spaces)."""
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def _iso(moment: datetime) -> str:
    return moment.astimezone(UTC).isoformat(timespec="microseconds")


def _iso_or_none(moment: datetime | None) -> str | None:
    return _iso(moment) if moment is not None else None


def _utc(moment: datetime) -> datetime:
    if moment.tzinfo is None:
        raise FailClosedError("ledger timestamps must carry a time zone")
    return moment.astimezone(UTC)


def _money_text(amount: Decimal) -> str:
    try:
        return str(amount.quantize(CENT)) if amount.is_finite() else str(amount)
    except InvalidOperation:
        return str(amount)


def _sum(rows: Iterable[tuple[str, ...]]) -> Decimal:
    return sum((Decimal(row[0]) for row in rows), Decimal("0"))


def _payload_hash(proposal: Proposal) -> str:
    fields = {
        "tool": proposal.tool,
        "amount": str(proposal.amount),
        "currency": proposal.currency,
        "order_id": proposal.order_id,
        "dispute_id": proposal.dispute_id,
        "payee_id": proposal.payee_id,
        "reference": proposal.reference,
        "claimed_captured": str(proposal.claimed_captured),
        "claimed_refunded": str(proposal.claimed_refunded),
    }
    return hashlib.sha256(canonical_json(fields).encode("utf-8")).hexdigest()


def _entry_hash(
    prev: str, created_at: str, request_id: str, event: str, body: str
) -> str:
    material = canonical_json([prev, created_at, request_id, event, body])
    return hashlib.sha256(material.encode("utf-8")).hexdigest()


def _reused_id_result() -> PolicyResult:
    return PolicyResult(
        decision=Decision.DENY,
        rule_id="R12",
        explanation=(
            "Request is invalid or incomplete: this request id was already used "
            "for a different request."
        ),
        triggered_rules=("R12",),
        metadata={"hits": [{"rule": "R12", "decision": "deny", "code": "id_reused"}]},
    )


def _approval_outcome(
    fresh: PolicyResult, drift: PolicyResult | None
) -> tuple[ProposalState, PolicyResult]:
    """Where an approved request goes: the strictest of the fresh decision and the
    change in the order since it was decided.

    The owner's approval answers "ask a person", so a fresh allow or ask goes ahead;
    a refusal or a hold always wins over the approval.
    """
    if fresh.decision is Decision.DENY:
        return S.DENIED, fresh
    if drift is not None and drift.decision is Decision.DENY:
        return S.DENIED, drift
    if fresh.decision is Decision.PAUSE:
        return S.PAUSED, fresh
    if drift is not None:
        return S.PENDING_APPROVAL, drift
    return S.EXECUTING, fresh


def _require_time_left(changed: str, decide_within: int | None, now: datetime) -> None:
    """Refuses an approval with no deadline given, or at or after its deadline."""
    if decide_within is None:
        raise FailClosedError("an approval must be checked against its deadline")
    deadline = datetime.fromisoformat(changed) + timedelta(seconds=decide_within)
    if now >= deadline:
        raise FailClosedError("the time to decide this request has passed")


class Ledger:
    """SQLite-backed ledger. One write transaction per decision or transition."""

    def __init__(
        self,
        path: Path | str,
        secret: bytes,
        policy: PolicySettings,
        expected_account_id: str,
    ) -> None:
        if len(secret) < 32:
            raise FailClosedError("the audit key must be at least 32 bytes")
        self._path = str(path)
        self._secret = secret
        self._policy = policy
        self._account = expected_account_id
        # the newest chain entry already checked, so a request's history checks
        # only what was added since; the scheduled full check covers the rest
        self._checked: tuple[int, str] | None = None
        self._check_lock = threading.Lock()
        with self._connection() as conn:
            conn.execute("PRAGMA journal_mode=WAL")
            conn.executescript(_SCHEMA)
            columns = {row["name"] for row in conn.execute(_Q_COLUMNS)}
            for column, statement in _MIGRATIONS.items():
                if column not in columns:
                    conn.execute(statement)
            conn.execute(_Q_BACKFILL)

    @property
    def account_id(self) -> str:
        """The PayPal merchant account every decision is checked against."""
        return self._account

    # ------------------------------------------------------------ connections

    @contextmanager
    def _connection(self) -> Iterator[sqlite3.Connection]:
        conn = sqlite3.connect(
            self._path, timeout=BUSY_TIMEOUT_SECONDS, isolation_level=None
        )
        conn.row_factory = sqlite3.Row
        try:
            yield conn
        finally:
            conn.close()

    @property
    def path(self) -> str:
        """The database file holding the ledger."""
        return self._path

    def append_event(
        self,
        conn: sqlite3.Connection,
        request_id: str,
        event: str,
        body: dict[str, Any],
        now: datetime,
    ) -> None:
        """Add an entry to the chain inside a transaction the caller holds."""
        self._append(conn, request_id, event, body, now)

    @contextmanager
    def _transaction(self) -> Iterator[sqlite3.Connection]:
        """An immediate write transaction: concurrent writers wait their turn."""
        with self._connection() as conn:
            conn.execute("BEGIN IMMEDIATE")
            committed = False
            try:
                yield conn
                conn.execute("COMMIT")
                committed = True
            finally:
                if not committed:
                    conn.execute("ROLLBACK")

    # ----------------------------------------------------------- emergency stop

    @staticmethod
    def _stopped(conn: sqlite3.Connection) -> bool:
        row = conn.execute(_Q_CONTROL_GET, (STOP_CONTROL,)).fetchone()
        return row is not None and row["value"] == "yes"

    def rule_of(self, request_id: str) -> str | None:
        """The rule that decided a request, or None if it is unknown."""
        with self._connection() as conn:
            row = conn.execute(_Q_RULE, (request_id,)).fetchone()
        return str(row["rule_id"]) if row is not None else None

    def reconciliation_inputs(
        self, since: datetime
    ) -> tuple[list[OurRefund], list[str], Known]:
        """Refunds we sent since a moment, every order touched since then, and
        every refund request ever made on those orders, so our own older or
        unsent requests are never mistaken for refunds made in PayPal directly."""
        refund_tool = ToolAction.CREATE_REFUND.value
        params = (refund_tool, S.EXECUTED.value, _iso(since))
        with self._connection() as conn:
            rows = conn.execute(_Q_RECONCILE_OURS, params).fetchall()
            orders = conn.execute(
                _Q_RECONCILE_ORDERS, (_iso(since), _iso(since))
            ).fetchall()
            order_ids = [str(row["order_id"]) for row in orders]
            made = conn.execute(
                _Q_RECONCILE_KNOWN, (refund_tool, json.dumps(order_ids))
            ).fetchall()
        sent = [
            OurRefund(
                request_id=str(row["request_id"]),
                order_id=str(row["order_id"] or ""),
                amount=Decimal(row["amount"]),
                paypal_id=str(row["paypal_id"] or ""),
                sent_at=datetime.fromisoformat(row["updated_at"]),
            )
            for row in rows
        ]
        counted = {str(r["request_id"]) for r in made if r["state"] in _SENT_STATES}
        known = Known(
            counted=frozenset(counted),
            not_sent=frozenset(str(r["request_id"]) for r in made) - counted,
            paypal_ids=frozenset(str(r["paypal_id"]) for r in made if r["paypal_id"]),
        )
        return sent, order_ids, known

    def record_webhook(
        self, event_id: str, event_type: str, resource_id: str, now: datetime
    ) -> bool:
        """Record a verified PayPal event once; False if it was seen before."""
        with self._transaction() as conn:
            seen = conn.execute(_Q_WEBHOOK_SEEN, (event_id, event_type, _iso(now)))
            if seen.rowcount == 0:
                return False
            body = {
                "event_id": event_id,
                "event_type": event_type,
                "resource_id": resource_id,
            }
            self._append(conn, WEBHOOK_REQUEST, "webhook_received", body, now)
            return True

    def record_reconciliation(self, summary: dict[str, Any], now: datetime) -> None:
        """Record one run's counts in the chain."""
        with self._transaction() as conn:
            self._append(conn, RECONCILE_REQUEST, "reconciled", summary, now)

    def last_reconciliation(self) -> dict[str, Any] | None:
        """The most recent reconciliation run, from the chain, or None."""
        runs = self.events(RECONCILE_REQUEST)
        return runs[-1] if runs else None

    def refunds_stopped(self) -> bool:
        """True while the merchant has stopped all refunds."""
        with self._connection() as conn:
            return self._stopped(conn)

    def set_refunds_stopped(self, stopped: bool, by: str, now: datetime) -> bool:
        """Stop or resume all refunds; returns False when nothing changed.

        The change is written to the chain in the same transaction, so the
        stored state can always be proven from the audit trail.
        """
        with self._transaction() as conn:
            if self._stopped(conn) == stopped:
                return False
            value = "yes" if stopped else "no"
            conn.execute(_Q_CONTROL_SET, (STOP_CONTROL, value, _iso(now)))
            event = "refunds_stopped" if stopped else "refunds_resumed"
            self._append(conn, CONTROL_REQUEST, event, {"by": by}, now)
            return True

    # --------------------------------------------------------------- decisions

    def decide(
        self,
        proposal: Proposal,
        evidence: OrderEvidence | None,
        signals: Signals,
        screening: ScreeningSettings,
        now: datetime,
    ) -> Recorded:
        """Decide and record atomically, or replay the outcome of a repeated id."""
        now = _utc(now)
        digest = _payload_hash(proposal)
        storable = REQUEST_ID_PATTERN.fullmatch(proposal.request_id or "") is not None
        with self._transaction() as conn:
            if storable:
                row = conn.execute(_Q_GET, (proposal.request_id,)).fetchone()
                if row is not None:
                    return self._replay(row, digest)
            context = self._context(conn, proposal, evidence, now)
            result = evaluate(
                proposal, evidence, context, signals, self._policy, screening, now
            )
            if not storable:
                return Recorded(proposal.request_id, None, result)
            state = STATE_FOR_DECISION[result.decision]
            self._insert(conn, proposal, evidence, digest, result, state, now)
            conn.execute(
                _Q_STAMP,
                (_iso(now), _iso_or_none(result.release_at), proposal.request_id),
            )
            self._append(
                conn,
                proposal.request_id,
                "decided",
                _audit_body(proposal, result, state),
                now,
            )
        return Recorded(proposal.request_id, state, result)

    def _replay(self, row: sqlite3.Row, digest: str) -> Recorded:
        if not hmac.compare_digest(row["payload_hash"], digest):
            return Recorded(row["request_id"], None, _reused_id_result())
        decision = Decision(row["decision"])
        release = row["release_at"]
        result = PolicyResult(
            decision=decision,
            rule_id=row["rule_id"],
            explanation=row["explanation"],
            requires_human_approval=decision is Decision.ESCALATE,
            triggered_rules=tuple(json.loads(row["triggered"])),
            release_at=datetime.fromisoformat(release) if release else None,
        )
        return Recorded(row["request_id"], ProposalState(row["state"]), result, True)

    def _context(
        self,
        conn: sqlite3.Connection,
        proposal: Proposal,
        evidence: OrderEvidence | None,
        now: datetime,
    ) -> LedgerContext:
        """Counters the policy needs, read inside the same write transaction.

        The proposal's own row (if it exists, as for a released pause) is left out,
        so a request never counts against itself.
        """
        own = proposal.request_id
        day_ago = _iso(now - VELOCITY_WINDOW)
        refund = ToolAction.CREATE_REFUND.value
        reserved = _sum(
            conn.execute(
                _Q_ORDER_RESERVED,
                (
                    proposal.order_id,
                    refund,
                    ToolAction.ACCEPT_DISPUTE_CLAIM.value,
                    *RESERVING,
                    own,
                ),
            )
        )
        outflow = _sum(conn.execute(_Q_OUTFLOW, (day_ago, *OUTFLOW, own)))
        customer = evidence.customer_key if evidence is not None else None
        times: tuple[datetime, ...] = ()
        if customer:
            params = (customer, refund, day_ago, *CUSTOMER_REFUNDS, own)
            rows = conn.execute(_Q_CUSTOMER, params).fetchall()
            times = tuple(datetime.fromisoformat(r["decided_at"]) for r in rows)
        refunds = len(times)
        known, duplicate = self._payee_facts(conn, proposal, now)
        auto_order, auto_customer = self._auto_facts(conn, proposal, customer, now)
        return LedgerContext(
            refunds_stopped=self._stopped(conn),
            expected_account_id=self._account,
            reserved_for_order=reserved,
            refunds_24h_for_customer=refunds,
            customer_refund_times=times,
            outflow_24h=outflow,
            auto_for_order=auto_order,
            auto_24h_for_customer=auto_customer,
            known_payee=known,
            duplicate_payout_in_window=duplicate,
        )

    def _auto_facts(
        self,
        conn: sqlite3.Connection,
        proposal: Proposal,
        customer: str | None,
        now: datetime,
    ) -> tuple[Decimal, Decimal]:
        """Refunds sent without a person, on this order and for this customer today.

        Only requests the rules let through on their own count: a refund the owner
        approved, or one that failed at PayPal, uses none of the automatic budget.
        """
        common = (ToolAction.CREATE_REFUND.value, Decision.ALLOW.value)
        sent = (S.EXECUTING, S.EXECUTED)
        own = proposal.request_id
        order = _sum(
            conn.execute(_Q_AUTO_ORDER, (proposal.order_id, *common, *sent, own))
        )
        if not customer:
            return order, Decimal("0")
        day_ago = _iso(now - VELOCITY_WINDOW)
        params = (customer, *common, day_ago, *sent, own)
        return order, _sum(conn.execute(_Q_AUTO_CUSTOMER, params))

    def _payee_facts(
        self, conn: sqlite3.Connection, proposal: Proposal, now: datetime
    ) -> tuple[bool, bool]:
        if not proposal.payee_id:
            return False, False
        key = payee_key(self._secret, proposal.payee_id)
        known = conn.execute(_Q_KNOWN_PAYEE, (key, S.EXECUTED)).fetchone() is not None
        window = _iso(now - timedelta(days=self._policy.payout_duplicate_window_days))
        params = (
            key,
            _money_text(proposal.amount),
            proposal.reference,
            window,
            *OUTFLOW,
        )
        duplicate = conn.execute(_Q_DUPLICATE, params).fetchone() is not None
        return known, duplicate

    def _insert(
        self,
        conn: sqlite3.Connection,
        proposal: Proposal,
        evidence: OrderEvidence | None,
        digest: str,
        result: PolicyResult,
        state: ProposalState,
        now: datetime,
    ) -> None:
        payee = (
            payee_key(self._secret, proposal.payee_id) if proposal.payee_id else None
        )
        stamp = _iso(now)
        conn.execute(
            _Q_INSERT,
            (
                proposal.request_id,
                digest,
                proposal.tool,
                proposal.order_id,
                evidence.customer_key if evidence is not None else None,
                payee,
                proposal.reference,
                _money_text(proposal.amount),
                proposal.currency,
                state.value,
                result.decision.value,
                result.rule_id,
                result.explanation,
                json.dumps(list(result.triggered_rules)),
                canonical_json(proposal_to_dict(proposal, self._secret)),
                canonical_json(evidence_to_dict(evidence)) if evidence else None,
                stamp,
                stamp,
            ),
        )

    # --------------------------------------------------------------- execution

    def begin_execution(self, proposal: Proposal, now: datetime) -> int:
        """Claim a proposal for execution; return how many attempts came before.

        Only a proposal that was allowed or approved (state executing) and whose
        content matches the recorded decision exactly may move money. Anything
        else is refused, so a caller cannot execute a pending or denied request
        or change the amount, order or recipient after the decision.
        """
        now = _utc(now)
        digest = _payload_hash(proposal)
        with self._transaction() as conn:
            row = conn.execute(_Q_GET, (proposal.request_id,)).fetchone()
            if row is None or not hmac.compare_digest(row["payload_hash"], digest):
                raise FailClosedError("proposal does not match the recorded decision")
            if ProposalState(row["state"]) is not S.EXECUTING:
                raise FailClosedError(
                    "proposal is not approved for execution", {"state": row["state"]}
                )
            previous = int(row["attempts"])
            conn.execute(_Q_ATTEMPT, (_iso(now), proposal.request_id))
            body = {"attempt": str(previous + 1)}
            self._append(conn, proposal.request_id, "execution_attempt", body, now)
        return previous

    # ------------------------------------------------------------- transitions

    def transition(
        self,
        request_id: str,
        to_state: ProposalState,
        now: datetime,
        detail: dict[str, str] | None = None,
        *,
        decide_within: int | None = None,
    ) -> None:
        """Move a proposal along its lifecycle; anything not allowed is refused.

        An approval must name the time allowed to decide and is refused once it
        has passed, inside the same transaction, so a late answer never sends
        money even before the expiry job has run.
        """
        now = _utc(now)
        with self._transaction() as conn:
            row = conn.execute(_Q_GET, (request_id,)).fetchone()
            if row is None:
                raise FailClosedError("unknown request id", {"request_id": request_id})
            current = ProposalState(row["state"])
            if to_state is S.EXECUTING and self._stopped(conn):
                raise FailClosedError("refunds are stopped, so nothing can be sent")
            if current is S.PENDING_APPROVAL and to_state is S.EXECUTING:
                _require_time_left(row["updated_at"], decide_within, now)
            if to_state not in ALLOWED_TRANSITIONS.get(current, frozenset()):
                raise FailClosedError(
                    "transition not allowed",
                    {"from": current.value, "to": to_state.value},
                )
            conn.execute(_Q_SET_STATE, (to_state.value, _iso(now), request_id))
            body = {"from": current.value, "to": to_state.value, **(detail or {})}
            self._append(conn, request_id, "state_changed", body, now)
            if to_state in DROPPED_OUT and current in CUSTOMER_REFUNDS:
                self._recheck_pauses(conn, row["customer_key"], now)

    def approve(
        self,
        request_id: str,
        evidence: OrderEvidence | None,
        signals: Signals,
        screening: ScreeningSettings,
        now: datetime,
        detail: dict[str, str],
        *,
        decide_within: int | None,
    ) -> Recorded:
        """Apply the owner's approval after deciding the request again from fresh facts.

        A request can wait hours, so the approval is checked against PayPal's record
        and our refunds as they are now, in the same transaction that checks the stop
        and the deadline. If the rules now refuse or hold it, the approval moves no
        money. If the order changed but the refund still fits, the owner is asked
        again with the new figures, because they approved figures that are gone.
        """
        now = _utc(now)
        with self._transaction() as conn:
            row = conn.execute(_Q_GET, (request_id,)).fetchone()
            if row is None or ProposalState(row["state"]) is not S.PENDING_APPROVAL:
                raise FailClosedError("this request is not waiting for approval")
            if self._stopped(conn):
                raise FailClosedError("refunds are stopped, so nothing can be sent")
            _require_time_left(row["updated_at"], decide_within, now)
            proposal, state, result = self._decide_again(
                conn, row, evidence, signals, screening, now
            )
            self._apply_approval(conn, row, proposal, evidence, result, state, now)
            body = {
                "from": S.PENDING_APPROVAL.value,
                "to": state.value,
                **({} if state is S.EXECUTING else {"recheck": result.rule_id}),
                **detail,
            }
            self._append(conn, request_id, "state_changed", body, now)
            if state in DROPPED_OUT or state is S.PAUSED:
                self._recheck_pauses(conn, row["customer_key"], now)
        return Recorded(request_id, state, result)

    def _decide_again(
        self,
        conn: sqlite3.Connection,
        row: sqlite3.Row,
        evidence: OrderEvidence | None,
        signals: Signals,
        screening: ScreeningSettings,
        now: datetime,
    ) -> tuple[Proposal, ProposalState, PolicyResult]:
        """Every rule on today's facts, plus what changed since the decision."""
        proposal = proposal_from_dict(json.loads(row["proposal_json"]))
        if evidence is not None and evidence.order_id != proposal.order_id:
            raise FailClosedError("approval evidence is for a different order")
        context = self._context(conn, proposal, evidence, now)
        fresh = evaluate(
            proposal, evidence, context, signals, self._policy, screening, now
        )
        decided = row["evidence_json"]
        drift = (
            check_execution_drift(evidence_from_dict(json.loads(decided)), evidence)
            if decided and evidence is not None
            else None
        )
        return (proposal, *_approval_outcome(fresh, drift))

    def _apply_approval(
        self,
        conn: sqlite3.Connection,
        row: sqlite3.Row,
        proposal: Proposal,
        evidence: OrderEvidence | None,
        result: PolicyResult,
        state: ProposalState,
        now: datetime,
    ) -> None:
        snapshot = canonical_json(evidence_to_dict(evidence)) if evidence else None
        if state in (S.EXECUTING, S.PENDING_APPROVAL):
            # the decision stands; the facts it is sent on are now PayPal's latest
            conn.execute(
                _Q_REBASE,
                (
                    state.value,
                    snapshot or row["evidence_json"],
                    _iso(now),
                    proposal.request_id,
                ),
            )
            return
        conn.execute(
            _Q_REDECIDE,
            (
                state.value,
                result.decision.value,
                result.rule_id,
                result.explanation,
                json.dumps(list(result.triggered_rules)),
                snapshot,
                _iso(now),
                _iso_or_none(result.release_at),
                _iso(now),
                proposal.request_id,
            ),
        )

    def _recheck_pauses(
        self, conn: sqlite3.Connection, customer: str | None, now: datetime
    ) -> None:
        if customer:
            stamp = _iso(now)
            conn.execute(_Q_RECHECK_PAUSES, (stamp, S.PAUSED.value, customer, stamp))

    def reserved_on_order(self, order_id: str, exclude: str) -> Decimal:
        """Money other requests on this order are waiting to send."""
        refund = ToolAction.CREATE_REFUND.value
        concession = ToolAction.ACCEPT_DISPUTE_CLAIM.value
        with self._connection() as conn:
            rows = conn.execute(
                _Q_ORDER_RESERVED,
                (order_id, refund, concession, *RESERVING, exclude),
            )
            return _sum(rows)

    def send_back_for_approval(
        self,
        request_id: str,
        current: OrderEvidence,
        now: datetime,
        detail: dict[str, str],
    ) -> None:
        """Return an executing proposal to the approver with the order as it is now.

        Nothing was sent to PayPal in the attempt that found the change, so the
        attempt count starts again and the next approval is judged against the
        order the approver actually saw, not the one first decided on.
        """
        now = _utc(now)
        snapshot = canonical_json(evidence_to_dict(current))
        with self._transaction() as conn:
            row = conn.execute(_Q_GET, (request_id,)).fetchone()
            if row is None or ProposalState(row["state"]) is not S.EXECUTING:
                raise FailClosedError("only an executing proposal can be sent back")
            conn.execute(
                _Q_REBASE, (S.PENDING_APPROVAL.value, snapshot, _iso(now), request_id)
            )
            body = {"from": S.EXECUTING.value, "to": S.PENDING_APPROVAL.value, **detail}
            self._append(conn, request_id, "state_changed", body, now)

    def timing(self, request_id: str) -> tuple[ProposalState, str, str] | None:
        """State, release time and last change of a proposal, for status messages."""
        with self._connection() as conn:
            row = conn.execute(_Q_GET, (request_id,)).fetchone()
        if row is None:
            return None
        return ProposalState(row["state"]), row["release_at"] or "", row["updated_at"]

    def mark_executed(
        self, request_id: str, now: datetime, paypal_id: str, paypal_status: str
    ) -> None:
        """Record PayPal's refund id and status with the move to executed."""
        detail = {"paypal_id": paypal_id, "paypal_status": paypal_status}
        self.transition(request_id, S.EXECUTED, now, detail)
        with self._transaction() as conn:
            conn.execute(
                _Q_PAYPAL, (paypal_id, paypal_status, _iso(_utc(now)), request_id)
            )

    def mark_failed_by_paypal(
        self, request_id: str, now: datetime, paypal_id: str, paypal_status: str
    ) -> None:
        """PayPal answered the refund with a failure: no money moved."""
        detail = {
            "reason": "paypal_failed",
            "paypal_id": paypal_id,
            "paypal_status": paypal_status,
        }
        self.transition(request_id, S.FAILED, now, detail)
        with self._transaction() as conn:
            conn.execute(
                _Q_PAYPAL, (paypal_id, paypal_status, _iso(_utc(now)), request_id)
            )

    def unsettled(self) -> list[str]:
        """Executed refunds PayPal still reports as pending."""
        with self._connection() as conn:
            rows = conn.execute(
                _Q_UNSETTLED, (S.EXECUTED.value, UNSETTLED_STATUSES[0])
            ).fetchall()
        return [str(row["request_id"]) for row in rows]

    def record_paypal_status(
        self, request_id: str, paypal_id: str, status: str, now: datetime
    ) -> ProposalState:
        """Store PayPal's latest status for an executed refund.

        A late failure or cancellation moves the request to failed: no money
        reached the customer, so nobody may be told it did.
        """
        now = _utc(now)
        if status in LATE_FAILURE_STATUSES:
            self.transition(
                request_id,
                S.FAILED,
                now,
                {"reason": "paypal_failed_later", "paypal_status": status},
            )
        with self._transaction() as conn:
            conn.execute(_Q_PAYPAL, (paypal_id, status, _iso(now), request_id))
            if status not in LATE_FAILURE_STATUSES:
                body = {"paypal_id": paypal_id, "paypal_status": status}
                self._append(conn, request_id, "paypal_status", body, now)
        return S.FAILED if status in LATE_FAILURE_STATUSES else S.EXECUTED

    def paypal_status(self, request_id: str) -> str:
        """PayPal's last reported status for this request's refund, or blank."""
        with self._connection() as conn:
            row = conn.execute(_Q_GET, (request_id,)).fetchone()
        return str(row["paypal_status"] or "") if row else ""

    def late_failures(self, now: datetime) -> list[dict[str, str]]:
        """Refunds PayPal failed or cancelled after accepting them, last 24 hours."""
        day_ago = _iso(_utc(now) - VELOCITY_WINDOW)
        params = (S.FAILED.value, *LATE_FAILURE_STATUSES, day_ago)
        with self._connection() as conn:
            rows = conn.execute(_Q_LATE_FAILURES, params).fetchall()
        return [{k: str(v or "") for k, v in dict(row).items()} for row in rows]

    def version(self) -> int:
        """A number that grows with every recorded change, for live updates."""
        with self._connection() as conn:
            return int(conn.execute(_Q_VERSION).fetchone()[0])

    def events(self, request_id: str) -> list[dict[str, Any]]:
        """The audit entries for one request, oldest first, bodies decoded."""
        with self._connection() as conn:
            rows = conn.execute(_Q_EVENTS, (request_id,)).fetchall()
        return [
            {"at": row["created_at"], "event": row["event"], **json.loads(row["body"])}
            for row in rows
        ]

    def refunded_through_app(self, order_id: str) -> Decimal:
        """Refunds on this order that this gateway sent and PayPal accepted."""
        refund = ToolAction.CREATE_REFUND.value
        with self._connection() as conn:
            rows = conn.execute(_Q_APP_REFUNDED, (order_id, refund, S.EXECUTED.value))
            return _sum(rows)

    def recent_outcomes(self, now: datetime) -> list[dict[str, str]]:
        """Requests that ended in the last 24 hours, newest first."""
        day_ago = _iso(_utc(now) - VELOCITY_WINDOW)
        ended = (S.EXECUTED.value, S.DENIED.value, S.EXPIRED.value, S.FAILED.value)
        with self._connection() as conn:
            rows = conn.execute(_Q_RECENT, (*ended, day_ago, RECENT_LIMIT)).fetchall()
        return [{k: str(v or "") for k, v in dict(row).items()} for row in rows]

    def recent_count(self, now: datetime) -> int:
        """How many requests ended in the last 24 hours, with no list limit."""
        day_ago = _iso(_utc(now) - VELOCITY_WINDOW)
        ended = (S.EXECUTED.value, S.DENIED.value, S.EXPIRED.value, S.FAILED.value)
        with self._connection() as conn:
            return int(conn.execute(_Q_RECENT_COUNT, (*ended, day_ago)).fetchone()[0])

    def day_summary(self, now: datetime) -> dict[str, str]:
        """The merchant's last 24 hours of refunds, from this gateway's record.

        A refund counts as refunded only once PayPal confirmed it; one PayPal is
        still finishing is counted apart, so the day never claims money moved
        early. "By your assistant" means sent without a person because it was
        within the automatic limit.
        """
        day_ago = _iso(_utc(now) - VELOCITY_WINDOW)
        refund = ToolAction.CREATE_REFUND.value
        sent = by_assistant = processing = Decimal("0")
        counts = {
            "sent": 0,
            "by_assistant": 0,
            "by_you": 0,
            "not_sent": 0,
            "processing": 0,
        }
        with self._connection() as conn:
            rows = conn.execute(_Q_DAY, (refund, day_ago)).fetchall()
            outflow = _sum(conn.execute(_Q_OUTFLOW, (day_ago, *OUTFLOW, "")))
            waiting = _sum(conn.execute(_Q_WAITING, (day_ago, *RESERVING)))
        for row in rows:
            state = ProposalState(row["state"])
            amount = Decimal(row["amount"])
            if state is S.EXECUTED and row["paypal_status"] != COMPLETED_STATUS:
                counts["processing"] += 1
                processing += amount
            elif state is S.EXECUTED:
                counts["sent"] += 1
                sent += amount
                if row["decision"] == Decision.ALLOW.value:
                    counts["by_assistant"] += 1
                    by_assistant += amount
                else:
                    counts["by_you"] += 1
            elif state in (S.DENIED, S.EXPIRED, S.FAILED):
                counts["not_sent"] += 1
        return {
            **{f"{key}_count": str(value) for key, value in counts.items()},
            "sent_total": f"{sent:.2f}",
            "by_assistant_total": f"{by_assistant:.2f}",
            "processing_total": f"{processing:.2f}",
            "limit_used": f"{outflow:.2f}",
            # money set aside for requests still open, already counted in the limit
            "waiting_total": f"{waiting:.2f}",
        }

    def releases_due(self, now: datetime) -> list[str]:
        """Paused requests whose release time has come, oldest pause first."""
        with self._connection() as conn:
            rows = conn.execute(
                _Q_RELEASE_DUE, (S.PAUSED.value, _iso(_utc(now)))
            ).fetchall()
        return [str(row["request_id"]) for row in rows]

    def release(
        self,
        request_id: str,
        evidence: OrderEvidence | None,
        signals: Signals,
        screening: ScreeningSettings,
        now: datetime,
    ) -> Recorded:
        """Decide a paused request again from fresh facts, exactly as if new.

        Every rule applies again; the result may send it, ask a person, refuse it,
        or pause it once more with a new release time.
        """
        now = _utc(now)
        with self._transaction() as conn:
            row = conn.execute(_Q_GET, (request_id,)).fetchone()
            if row is None or ProposalState(row["state"]) is not S.PAUSED:
                raise FailClosedError("only a paused request can be released")
            if self._stopped(conn):
                raise FailClosedError("refunds are stopped, so it stays on hold")
            proposal = proposal_from_dict(json.loads(row["proposal_json"]))
            if evidence is not None and evidence.order_id != proposal.order_id:
                raise FailClosedError("release evidence is for a different order")
            context = self._context(conn, proposal, evidence, now)
            result = evaluate(
                proposal, evidence, context, signals, self._policy, screening, now
            )
            state = STATE_FOR_DECISION[result.decision]
            conn.execute(
                _Q_REDECIDE,
                (
                    state.value,
                    result.decision.value,
                    result.rule_id,
                    result.explanation,
                    json.dumps(list(result.triggered_rules)),
                    canonical_json(evidence_to_dict(evidence)) if evidence else None,
                    _iso(now),
                    _iso_or_none(result.release_at),
                    _iso(now),
                    request_id,
                ),
            )
            body = {"from": S.PAUSED.value, **_audit_body(proposal, result, state)}
            self._append(conn, request_id, "released", body, now)
        return Recorded(request_id, state, result)

    def paused(self, now: datetime) -> list[dict[str, str]]:
        """Paused requests for the merchant, with the customer's refunds and asks in
        the last 24 hours."""
        day_ago = _iso(_utc(now) - VELOCITY_WINDOW)
        refund = ToolAction.CREATE_REFUND.value
        items: list[dict[str, str]] = []
        with self._connection() as conn:
            for row in conn.execute(_Q_PAUSED, (S.PAUSED.value,)).fetchall():
                asks = refunds = 0
                if row["customer_key"]:
                    params = (row["customer_key"], refund, day_ago)
                    asks = int(conn.execute(_Q_CUSTOMER_ASKS, params).fetchone()[0])
                    counted = (*params, *CUSTOMER_REFUNDS)
                    refunds = int(
                        conn.execute(_Q_CUSTOMER_REFUND_COUNT, counted).fetchone()[0]
                    )
                item = {k: str(v or "") for k, v in dict(row).items()}
                item.pop("customer_key")
                item["requests_24h"] = str(asks)
                item["refunds_24h"] = str(refunds)
                items.append(item)
        return items

    def record_greeting(self, request_id: str, named: bool) -> None:
        """Keep whether the customer was greeted by name; never the name."""
        with self._connection() as conn:
            conn.execute(_Q_GREETING, ("yes" if named else "no", request_id))

    def same_paused_request(self, proposal: Proposal) -> str | None:
        """An existing pause for the same order, action and amount, if any."""
        with self._connection() as conn:
            row = conn.execute(
                _Q_SAME_PAUSED,
                (
                    S.PAUSED.value,
                    proposal.order_id,
                    proposal.tool,
                    _money_text(proposal.amount),
                ),
            ).fetchone()
        return str(row["request_id"]) if row else None

    def expire_due(self, now: datetime, open_seconds: int) -> list[str]:
        """Expire approvals left waiting too long, releasing their reservations."""
        now = _utc(now)
        cutoff = _iso(now - timedelta(seconds=open_seconds))
        expired: list[str] = []
        with self._transaction() as conn:
            for row in conn.execute(_Q_DUE, (S.PENDING_APPROVAL, cutoff)).fetchall():
                request_id = row["request_id"]
                conn.execute(_Q_SET_STATE, (S.EXPIRED.value, _iso(now), request_id))
                body = {"from": S.PENDING_APPROVAL.value, "to": S.EXPIRED.value}
                self._append(conn, request_id, "state_changed", body, now)
                self._recheck_pauses(conn, row["customer_key"], now)
                expired.append(request_id)
        return expired

    def load(self, request_id: str) -> tuple[Proposal, OrderEvidence | None] | None:
        """The proposal exactly as decided, and the evidence the decision used."""
        with self._connection() as conn:
            row = conn.execute(_Q_GET, (request_id,)).fetchone()
        if row is None or row["proposal_json"] is None:
            return None
        proposal = proposal_from_dict(json.loads(row["proposal_json"]))
        evidence_text = row["evidence_json"]
        evidence = (
            evidence_from_dict(json.loads(evidence_text)) if evidence_text else None
        )
        return proposal, evidence

    def unfinished_executions(self, now: datetime, quiet_seconds: int) -> list[str]:
        """Executing proposals with no attempt for a while: PayPal's answer was lost.

        The quiet period keeps a retry from racing an attempt still in flight.
        """
        cutoff = _iso(_utc(now) - timedelta(seconds=quiet_seconds))
        with self._connection() as conn:
            rows = conn.execute(_Q_STALE, (S.EXECUTING.value, cutoff)).fetchall()
        return [str(row["request_id"]) for row in rows]

    def sending(self) -> list[dict[str, str]]:
        """Proposals approved or allowed and still waiting for PayPal's answer."""
        with self._connection() as conn:
            rows = conn.execute(_Q_SENDING, (S.EXECUTING.value,)).fetchall()
        return [
            {key: str(value or "") for key, value in dict(row).items()} for row in rows
        ]

    def pending_approvals(self) -> list[dict[str, str]]:
        """Proposals waiting for a person, oldest first, as plain text fields."""
        with self._connection() as conn:
            rows = conn.execute(_Q_PENDING, (S.PENDING_APPROVAL.value,)).fetchall()
        return [
            {key: str(value or "") for key, value in dict(row).items()} for row in rows
        ]

    def action_digest(self, request_id: str) -> str | None:
        """Digest of the exact recorded action, for binding an approval to it."""
        with self._connection() as conn:
            row = conn.execute(_Q_GET, (request_id,)).fetchone()
        if row is None:
            return None
        facts = {
            "request_id": row["request_id"],
            "payload_hash": row["payload_hash"],
            "tool": row["tool"],
            "amount": row["amount"],
            "currency": row["currency"],
            "order_id": row["order_id"],
            "state": row["state"],
        }
        return hashlib.sha256(canonical_json(facts).encode("utf-8")).hexdigest()

    def state_of(self, request_id: str) -> ProposalState | None:
        """Current state of a proposal, or None if unknown."""
        with self._connection() as conn:
            row = conn.execute(_Q_GET, (request_id,)).fetchone()
        return ProposalState(row["state"]) if row is not None else None

    # ------------------------------------------------------------------- audit

    def _append(
        self,
        conn: sqlite3.Connection,
        request_id: str,
        event: str,
        body: dict[str, Any],
        now: datetime,
    ) -> None:
        last = conn.execute(_Q_LAST_HASH).fetchone()
        prev = last["entry_hash"] if last is not None else GENESIS_HASH
        created_at = _iso(now)
        text = canonical_json(body)
        entry = _entry_hash(prev, created_at, request_id, event, text)
        mac = hmac.new(self._secret, entry.encode("ascii"), hashlib.sha256).hexdigest()
        conn.execute(_Q_APPEND, (created_at, request_id, event, text, prev, entry, mac))

    def verify_chain(self) -> int:
        """Check every link and authentication tag; return the number of entries.

        Raises:
            AuditIntegrityError: at the first entry that was edited, removed,
                reordered or recomputed without the key.
        """
        with self._connection() as conn:
            rows = conn.execute(_Q_CHAIN).fetchall()
        self._verify_rows(rows)
        return len(rows)

    def _verify_rows(self, rows: list[sqlite3.Row]) -> None:
        """Check a whole chain, read once, from the first entry to the last."""
        prev = GENESIS_HASH
        for row in rows:
            self._verify_entry(row, prev)
            prev = row["entry_hash"]

    def _verify_entry(self, row: sqlite3.Row, prev: str) -> None:
        seq = row["seq"]
        if not hmac.compare_digest(row["prev_hash"], prev):
            raise AuditIntegrityError("audit chain is broken", {"seq": seq})
        expected = _entry_hash(
            prev, row["created_at"], row["request_id"], row["event"], row["body"]
        )
        if not hmac.compare_digest(row["entry_hash"], expected):
            raise AuditIntegrityError("audit entry was altered", {"seq": seq})
        mac = hmac.new(self._secret, expected.encode("ascii"), hashlib.sha256)
        if not hmac.compare_digest(row["mac"], mac.hexdigest()):
            raise AuditIntegrityError("audit entry is not authentic", {"seq": seq})

    def verify_records(self) -> int:
        """Check every proposal row against what the authenticated audit says.

        Detects a decision, amount or state edited directly in the proposals
        table. Returns the number of proposals checked.

        Raises:
            AuditIntegrityError: when the chain is broken or a row disagrees
                with the audit trail.
        """
        with self._connection() as conn:
            audit = conn.execute(_Q_CHAIN).fetchall()
            proposals = conn.execute(_Q_ALL_PROPOSALS).fetchall()
        # the same rows are checked and then marked as checked, so an entry
        # added meanwhile is never counted as checked
        self._verify_rows(audit)
        facts = _replayed_facts(audit)
        for row in proposals:
            _check_proposal(row, facts.get(row["request_id"]))
        self._verify_stop_state(audit)
        if audit:
            with self._check_lock:
                self._checked = (int(audit[-1]["seq"]), str(audit[-1]["entry_hash"]))
        return len(proposals)

    def verify_request(self, request_id: str) -> None:
        """Check one request's record without re-reading the whole chain.

        Its own entries are checked every time, the chain only from the newest
        entry already checked (which is checked again), and its stored row
        against its own entries. An edit to an older entry of another request is
        left to the scheduled full check in ``verify_records``.

        Raises:
            AuditIntegrityError: when anything this request relies on changed.
        """
        with self._check_lock, self._connection() as conn:
            self._verify_new_entries(conn)
            own = conn.execute(_Q_REQUEST_CHAIN, (request_id,)).fetchall()
            for row in own:
                self._verify_entry(row, self._prev_hash(conn, int(row["seq"])))
            proposal = conn.execute(_Q_ONE_PROPOSAL, (request_id,)).fetchone()
        if proposal is not None:
            _check_proposal(proposal, _replayed_facts(own).get(request_id))

    def _verify_new_entries(self, conn: sqlite3.Connection) -> None:
        """Check the chain from the newest entry already checked to the end."""
        prev, after = GENESIS_HASH, 0
        if self._checked is not None:
            after, prev = self._checked
            point = conn.execute(_Q_CHAIN_AT, (after,)).fetchone()
            if point is None or not hmac.compare_digest(point["entry_hash"], prev):
                raise AuditIntegrityError("an entry already checked was changed")
            self._verify_entry(point, self._prev_hash(conn, after))
        for row in conn.execute(_Q_CHAIN_AFTER, (after,)).fetchall():
            self._verify_entry(row, prev)
            prev, after = row["entry_hash"], int(row["seq"])
        self._checked = (after, prev) if after else None

    @staticmethod
    def _prev_hash(conn: sqlite3.Connection, seq: int) -> str:
        row = conn.execute(_Q_PREV_HASH, (seq,)).fetchone()
        return str(row["entry_hash"]) if row else GENESIS_HASH

    def _verify_stop_state(self, audit: list[sqlite3.Row]) -> None:
        """The stored stop switch must match the last stop or resume in the chain."""
        expected = "no"
        for row in audit:
            if row["request_id"] == CONTROL_REQUEST and row["event"] in STOP_EVENTS:
                expected = STOP_EVENTS[row["event"]]
        actual = "yes" if self.refunds_stopped() else "no"
        if expected != actual:
            raise AuditIntegrityError("the refund stop switch differs from the audit")

    def head(self) -> dict[str, Any]:
        """The latest chain entry, to publish where this service cannot edit it."""
        with self._connection() as conn:
            last = conn.execute(_Q_HEAD).fetchone()
        if last is None:
            return {"seq": 0, "entry_hash": GENESIS_HASH, "created_at": ""}
        return {
            "seq": int(last["seq"]),
            "entry_hash": str(last["entry_hash"]),
            "created_at": str(last["created_at"]),
        }

    def verify_against_head(self, seq: int, entry_hash: str) -> int:
        """Check the chain still contains a head published earlier.

        A chain rebuilt by someone holding the audit key is internally valid,
        but it cannot reproduce a hash that was published before the rewrite.

        Raises:
            AuditIntegrityError: when the chain is broken, shorter than the
                published head, or rewritten since it was published.
        """
        count = self.verify_chain()
        with self._connection() as conn:
            row = conn.execute(_Q_AT_SEQ, (seq,)).fetchone()
        if row is None:
            raise AuditIntegrityError(
                "the chain no longer has the published entry", {"seq": seq}
            )
        if not hmac.compare_digest(str(row["entry_hash"]), entry_hash):
            raise AuditIntegrityError(
                "the chain was rewritten since this head was published", {"seq": seq}
            )
        return count


def _check_proposal(row: sqlite3.Row, expected: dict[str, str] | None) -> None:
    """A stored request must match what its authenticated audit entries say."""
    actual = {
        "tool": row["tool"],
        "amount": _normal_amount(row["amount"]),
        "decision": row["decision"],
        "state": row["state"],
    }
    if expected is None or expected != actual:
        raise AuditIntegrityError(
            "a recorded decision differs from the audit trail",
            {"request_id": row["request_id"]},
        )


def _normal_amount(value: str) -> str:
    try:
        return str(Decimal(value).quantize(CENT))
    except InvalidOperation:
        return value


def _replayed_facts(rows: list[sqlite3.Row]) -> dict[str, dict[str, str]]:
    """Rebuild each proposal's recorded facts and latest state from the audit."""
    facts: dict[str, dict[str, str]] = {}
    for row in rows:
        body = json.loads(row["body"])
        request_id = row["request_id"]
        if row["event"] == "decided":
            facts[request_id] = {
                "tool": str(body["tool"]),
                "amount": _normal_amount(str(body["amount"])),
                "decision": str(body["decision"]),
                "state": str(body["state"]),
            }
        elif row["event"] == "state_changed" and request_id in facts:
            facts[request_id]["state"] = str(body["to"])
        elif row["event"] == "released" and request_id in facts:
            # a paused request decided again: its decision and state change
            facts[request_id]["decision"] = str(body["decision"])
            facts[request_id]["state"] = str(body["state"])
    return facts


def proposal_to_dict(proposal: Proposal, secret: bytes) -> dict[str, Any]:
    """Serialise a proposal with money as text and the payee only as a digest.

    Payee details are personal data and are never stored in clear. A reloaded
    payout then does not match its recorded content, so it cannot execute until
    encrypted payee storage exists.
    """
    out: dict[str, Any] = {}
    for name in proposal.__slots__:
        value = getattr(proposal, name)
        out[name] = str(value) if isinstance(value, Decimal) else value
    if proposal.payee_id:
        out["payee_id"] = payee_key(secret, proposal.payee_id)
    return out


def proposal_from_dict(data: dict[str, Any]) -> Proposal:
    """Rebuild a proposal serialised by ``proposal_to_dict``."""
    money = ("amount", "claimed_captured", "claimed_refunded")
    values: dict[str, Any] = {
        key: (Decimal(value) if key in money and value is not None else value)
        for key, value in data.items()
    }
    return Proposal(**values)


def evidence_to_dict(evidence: OrderEvidence) -> dict[str, Any]:
    """Serialise evidence with money as text and the time in ISO format."""
    out: dict[str, Any] = {}
    for name in evidence.__slots__:
        value = getattr(evidence, name)
        if isinstance(value, Decimal):
            value = str(value)
        elif isinstance(value, datetime):
            value = value.isoformat()
        out[name] = value
    return out


def evidence_from_dict(data: dict[str, Any]) -> OrderEvidence:
    """Rebuild evidence serialised by ``evidence_to_dict``."""
    money = ("captured_amount", "refunded_amount", "dispute_amount")
    values: dict[str, Any] = {}
    for key, value in data.items():
        if key in money and value is not None:
            value = Decimal(value)
        elif key == "fetched_at":
            value = datetime.fromisoformat(value)
        elif key == "tagged_refunds":
            value = tuple(tuple(item) for item in value or ())
        values[key] = value
    return OrderEvidence(**values)


def _audit_body(
    proposal: Proposal, result: PolicyResult, state: ProposalState
) -> dict[str, Any]:
    """What the audit trail records about a decision (no payee or card details)."""
    return {
        "tool": proposal.tool,
        "amount": str(proposal.amount),
        "currency": proposal.currency,
        "order_id": proposal.order_id,
        "decision": result.decision.value,
        "rule_id": result.rule_id,
        "triggered": list(result.triggered_rules),
        "state": state.value,
        **({"release_at": _iso(result.release_at)} if result.release_at else {}),
    }
