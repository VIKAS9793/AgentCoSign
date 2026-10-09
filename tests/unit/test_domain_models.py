"""Unit tests for domain models and enums."""

from datetime import UTC, datetime
from decimal import Decimal

from agentcosign.models.domain import (
    AuditEntry,
    Decision,
    PolicyResult,
    ToolAction,
)


def test_decision_enum_values() -> None:
    """Verify core policy decisions match contract."""
    assert Decision.ALLOW == "allow"
    assert Decision.ESCALATE == "escalate"
    assert Decision.DENY == "deny"


def test_tool_action_enum_values() -> None:
    """Verify tool action names match PayPal toolkit identifiers."""
    assert ToolAction.CREATE_REFUND == "create_refund"
    assert ToolAction.ACCEPT_DISPUTE_CLAIM == "accept_dispute_claim"
    assert ToolAction.PAYOUT == "payout"


def test_policy_result_creation() -> None:
    """Verify PolicyResult dataclass instantiates as immutable."""
    result = PolicyResult(
        decision=Decision.ALLOW,
        rule_id="R1",
        explanation="Refund under $25 auto-approved",
    )
    assert result.decision == Decision.ALLOW
    assert result.rule_id == "R1"
    assert not result.requires_human_approval


def test_audit_entry_structure() -> None:
    """Verify AuditEntry dataclass fields."""
    now = datetime.now(UTC)
    entry = AuditEntry(
        entry_id="audit_001",
        timestamp=now,
        previous_hash="0000000000000000000000000000000000000000000000000000000000000000",
        entry_hash="a1b2c3d4e5f67890a1b2c3d4e5f67890a1b2c3d4e5f67890a1b2c3d4e5f67890",
        action=ToolAction.CREATE_REFUND,
        decision=Decision.ALLOW,
        rule_id="R1",
        actor="ai_agent",
        amount=Decimal("15.00"),
        currency="USD",
        payload={"order_id": "ORD_123", "reason": "Customer return"},
        paypal_transaction_id="REF_999",
    )
    assert entry.amount == Decimal("15.00")
    assert entry.decision == Decision.ALLOW
    assert entry.paypal_transaction_id == "REF_999"
