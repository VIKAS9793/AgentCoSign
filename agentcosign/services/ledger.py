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
from agentcosign.services.policy_engine import evaluate

GENESIS_HASH = "0" * 64
BUSY_TIMEOUT_SECONDS = 5
CENT = Decimal("0.01")


class ProposalState(StrEnum):
    """Lifecycle of a proposal after the decision."""

    PENDING_APPROVAL = "pending_approval"
    EXECUTING = "executing"
    EXECUTED = "executed"
    DENIED = "denied"
    EXPIRED = "expired"
    FAILED = "failed"


S = ProposalState
RESERVING = (S.PENDING_APPROVAL, S.EXECUTING)
OUTFLOW = (S.PENDING_APPROVAL, S.EXECUTING, S.EXECUTED)
ALLOWED_TRANSITIONS: dict[ProposalState, frozenset[ProposalState]] = {
    S.PENDING_APPROVAL: frozenset({S.EXECUTING, S.DENIED, S.EXPIRED}),
    # Back to approval when the order's state moved between decision and execution.
    S.EXECUTING: frozenset({S.EXECUTED, S.FAILED, S.PENDING_APPROVAL}),
}
STATE_FOR_DECISION = {
    Decision.ALLOW: S.EXECUTING,
    Decision.ESCALATE: S.PENDING_APPROVAL,
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
"""

_Q_GET = "SELECT * FROM proposals WHERE request_id = ?"
_Q_ORDER_RESERVED = (
    "SELECT amount FROM proposals WHERE order_id = ? AND tool IN (?, ?) "
    "AND state IN (?, ?)"
)
_Q_OUTFLOW = "SELECT amount FROM proposals WHERE created_at >= ? AND state IN (?, ?, ?)"
_Q_CUSTOMER = (
    "SELECT COUNT(*) FROM proposals WHERE customer_key = ? AND tool = ? "
    "AND created_at >= ? AND state IN (?, ?, ?)"
)
_Q_KNOWN_PAYEE = "SELECT 1 FROM proposals WHERE payee_key = ? AND state = ? LIMIT 1"
_Q_DUPLICATE = (
    "SELECT 1 FROM proposals WHERE payee_key = ? AND amount = ? AND reference = ? "
    "AND created_at >= ? AND state IN (?, ?, ?) LIMIT 1"
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
_Q_DUE = "SELECT request_id FROM proposals WHERE state = ? AND created_at < ?"
_Q_ATTEMPT = (
    "UPDATE proposals SET attempts = attempts + 1, updated_at = ? WHERE request_id = ?"
)
_Q_COLUMNS = "PRAGMA table_info(proposals)"
_MIGRATIONS = {
    "attempts": "ALTER TABLE proposals ADD COLUMN attempts INTEGER NOT NULL DEFAULT 0",
    "proposal_json": "ALTER TABLE proposals ADD COLUMN proposal_json TEXT",
    "evidence_json": "ALTER TABLE proposals ADD COLUMN evidence_json TEXT",
}
_Q_PENDING = (
    "SELECT request_id, tool, amount, currency, order_id, rule_id, explanation, "
    "triggered, created_at FROM proposals WHERE state = ? ORDER BY created_at"
)
_Q_ALL_PROPOSALS = "SELECT request_id, tool, amount, decision, state FROM proposals"
_Q_LAST_HASH = "SELECT entry_hash FROM audit ORDER BY seq DESC LIMIT 1"
_Q_APPEND = (
    "INSERT INTO audit (created_at, request_id, event, body, prev_hash, entry_hash, "
    "mac) VALUES (?, ?, ?, ?, ?, ?, ?)"
)
_Q_CHAIN = (
    "SELECT seq, created_at, request_id, event, body, prev_hash, entry_hash, mac "
    "FROM audit ORDER BY seq"
)


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
        with self._connection() as conn:
            conn.execute("PRAGMA journal_mode=WAL")
            conn.executescript(_SCHEMA)
            columns = {row["name"] for row in conn.execute(_Q_COLUMNS)}
            for column, statement in _MIGRATIONS.items():
                if column not in columns:
                    conn.execute(statement)

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
        result = PolicyResult(
            decision=decision,
            rule_id=row["rule_id"],
            explanation=row["explanation"],
            requires_human_approval=decision is Decision.ESCALATE,
            triggered_rules=tuple(json.loads(row["triggered"])),
        )
        return Recorded(row["request_id"], ProposalState(row["state"]), result, True)

    def _context(
        self,
        conn: sqlite3.Connection,
        proposal: Proposal,
        evidence: OrderEvidence | None,
        now: datetime,
    ) -> LedgerContext:
        """Counters the policy needs, read inside the same write transaction."""
        day_ago = _iso(now - timedelta(hours=24))
        refund = ToolAction.CREATE_REFUND.value
        reserved = _sum(
            conn.execute(
                _Q_ORDER_RESERVED,
                (
                    proposal.order_id,
                    refund,
                    ToolAction.ACCEPT_DISPUTE_CLAIM.value,
                    *RESERVING,
                ),
            )
        )
        outflow = _sum(conn.execute(_Q_OUTFLOW, (day_ago, *OUTFLOW)))
        customer = evidence.customer_key if evidence is not None else None
        refunds = 0
        if customer:
            params = (customer, refund, day_ago, *OUTFLOW)
            refunds = int(conn.execute(_Q_CUSTOMER, params).fetchone()[0])
        known, duplicate = self._payee_facts(conn, proposal, now)
        return LedgerContext(
            expected_account_id=self._account,
            reserved_for_order=reserved,
            refunds_24h_for_customer=refunds,
            outflow_24h=outflow,
            known_payee=known,
            duplicate_payout_in_window=duplicate,
        )

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
    ) -> None:
        """Move a proposal along its lifecycle; anything not allowed is refused."""
        now = _utc(now)
        with self._transaction() as conn:
            row = conn.execute(_Q_GET, (request_id,)).fetchone()
            if row is None:
                raise FailClosedError("unknown request id", {"request_id": request_id})
            current = ProposalState(row["state"])
            if to_state not in ALLOWED_TRANSITIONS.get(current, frozenset()):
                raise FailClosedError(
                    "transition not allowed",
                    {"from": current.value, "to": to_state.value},
                )
            conn.execute(_Q_SET_STATE, (to_state.value, _iso(now), request_id))
            body = {"from": current.value, "to": to_state.value, **(detail or {})}
            self._append(conn, request_id, "state_changed", body, now)

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

    def expire_due(self, now: datetime, ttl_seconds: int) -> list[str]:
        """Expire approvals left waiting too long, releasing their reservations."""
        now = _utc(now)
        cutoff = _iso(now - timedelta(seconds=ttl_seconds))
        expired: list[str] = []
        with self._transaction() as conn:
            for row in conn.execute(_Q_DUE, (S.PENDING_APPROVAL, cutoff)).fetchall():
                request_id = row["request_id"]
                conn.execute(_Q_SET_STATE, (S.EXPIRED.value, _iso(now), request_id))
                body = {"from": S.PENDING_APPROVAL.value, "to": S.EXPIRED.value}
                self._append(conn, request_id, "state_changed", body, now)
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
        prev = GENESIS_HASH
        count = 0
        with self._connection() as conn:
            rows = conn.execute(_Q_CHAIN).fetchall()
        for row in rows:
            self._verify_entry(row, prev)
            prev = row["entry_hash"]
            count += 1
        return count

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
        self.verify_chain()
        with self._connection() as conn:
            audit = conn.execute(_Q_CHAIN).fetchall()
            proposals = conn.execute(_Q_ALL_PROPOSALS).fetchall()
        facts = _replayed_facts(audit)
        for row in proposals:
            expected = facts.get(row["request_id"])
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
        return len(proposals)

    def head_hash(self) -> str:
        """Hash of the latest audit entry, for publishing the chain head."""
        with self._connection() as conn:
            last = conn.execute(_Q_LAST_HASH).fetchone()
        return last["entry_hash"] if last is not None else GENESIS_HASH


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
    }
