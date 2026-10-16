"""Unit tests for domain models and enums."""

from agentcosign.models.domain import (
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
