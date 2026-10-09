"""Domain models and core enumeration types for AgentCoSign."""

from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from enum import StrEnum
from typing import Any


class Decision(StrEnum):
    """Outcomes produced by the policy engine."""

    ALLOW = "allow"
    ESCALATE = "escalate"
    DENY = "deny"


class ToolAction(StrEnum):
    """Supported money-moving tool actions in the PayPal ecosystem."""

    CREATE_REFUND = "create_refund"
    ACCEPT_DISPUTE_CLAIM = "accept_dispute_claim"
    PAYOUT = "payout"


class ApprovalStatus(StrEnum):
    """Status of human co-sign requests."""

    PENDING = "pending"
    APPROVED = "approved"
    REJECTED = "rejected"
    EXPIRED = "expired"


@dataclass(frozen=True)
class PolicyResult:
    """Result of policy evaluation for a proposed action.

    ``rule_id`` is the deciding (strictest) rule; ``triggered_rules`` lists every
    rule that fired so the audit log and approver can see the full picture.
    """

    decision: Decision
    rule_id: str
    explanation: str
    requires_human_approval: bool = False
    triggered_rules: tuple[str, ...] = ()
    metadata: dict[str, Any] | None = None


@dataclass(frozen=True)
class AuditEntry:
    """Immutable entry in the append-only hash-chained audit ledger."""

    entry_id: str
    timestamp: datetime
    previous_hash: str
    entry_hash: str
    action: ToolAction
    decision: Decision
    rule_id: str
    actor: str
    amount: Decimal
    currency: str
    payload: dict[str, Any]
    paypal_transaction_id: str | None = None
    approver_assurance: str | None = None  # "passkey" | "session" | None
