"""Domain models and core enumeration types for AgentCoSign."""

from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from typing import Any


class Decision(StrEnum):
    """Outcomes produced by the policy engine."""

    ALLOW = "allow"
    ESCALATE = "escalate"
    # Held until a known time, then decided again from fresh facts. Nobody can
    # send it before then; declining stays possible.
    PAUSE = "pause"
    DENY = "deny"


class ToolAction(StrEnum):
    """Supported money-moving tool actions in the PayPal ecosystem."""

    CREATE_REFUND = "create_refund"
    ACCEPT_DISPUTE_CLAIM = "accept_dispute_claim"
    PAYOUT = "payout"


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
    release_at: datetime | None = None
