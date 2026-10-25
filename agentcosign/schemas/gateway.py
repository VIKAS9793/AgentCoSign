"""Request and response shapes for the gateway API, validated at the edge."""

from decimal import Decimal
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

from agentcosign.models.policy import Proposal

MONEY = r"^\d{1,9}(\.\d{1,2})?$"
PAYPAL_ID = r"^[A-Z0-9-]{1,64}$"
REQUEST_ID = r"^[A-Za-z0-9._:-]{1,38}$"
MESSAGE_ID = r"^msg-[a-f0-9]{16}$"

_STRICT = ConfigDict(extra="forbid", frozen=True)


class ProposalIn(BaseModel):
    """What the agent asks the gateway to do. Every field is a claim."""

    model_config = _STRICT

    request_id: str = Field(pattern=REQUEST_ID)
    tool: Literal["create_refund", "accept_dispute_claim", "payout"]
    amount: str = Field(pattern=MONEY)
    currency: str = Field(pattern=r"^[A-Z]{3}$")
    order_id: str | None = Field(default=None, pattern=PAYPAL_ID)
    dispute_id: str | None = Field(default=None, pattern=PAYPAL_ID)
    payee_id: str | None = Field(default=None, min_length=1, max_length=128)
    reference: str | None = Field(default=None, min_length=1, max_length=64)
    claimed_captured: str | None = Field(default=None, pattern=MONEY)
    claimed_refunded: str | None = Field(default=None, pattern=MONEY)
    message_id: str | None = Field(default=None, pattern=MESSAGE_ID)

    def to_proposal(self) -> Proposal:
        """Convert to the domain object, with money as exact decimals."""

        def money(value: str | None) -> Decimal | None:
            """Exact decimal for an optional money field."""
            return Decimal(value) if value is not None else None

        return Proposal(
            request_id=self.request_id,
            tool=self.tool,
            amount=Decimal(self.amount),
            currency=self.currency,
            order_id=self.order_id,
            dispute_id=self.dispute_id,
            payee_id=self.payee_id,
            reference=self.reference,
            claimed_captured=money(self.claimed_captured),
            claimed_refunded=money(self.claimed_refunded),
            message_id=self.message_id,
        )


class OutcomeOut(BaseModel):
    """What happened to a proposal or an approval."""

    model_config = _STRICT

    request_id: str
    decision: str
    rule_id: str
    explanation: str
    state: str | None
    triggered: list[str]
    replayed: bool
    execution: str | None
    paypal_id: str | None
    paypal_status: str | None = None
    customer_message: str | None = None
    update_by: str | None = None
    merchant_reason: str | None = None


class SeenIn(BaseModel):
    """The order's figures on the owner's screen when they asked to approve."""

    model_config = _STRICT

    paid: str = Field(pattern=MONEY)
    already_refunded: str = Field(pattern=MONEY)


class IntentIn(BaseModel):
    """Whether the approver is about to approve or reject."""

    model_config = _STRICT

    intent: Literal["approve", "reject"]
    seen: SeenIn | None = None


class CredentialIn(BaseModel):
    """A passkey response produced by the browser."""

    model_config = _STRICT

    credential: dict[str, Any]


class EnrolStartIn(BaseModel):
    """Start enrolling an approver passkey."""

    model_config = _STRICT

    approver: str = Field(min_length=1, max_length=64)


class EnrolFinishIn(BaseModel):
    """Finish enrolling an approver passkey."""

    model_config = _STRICT

    approver: str = Field(min_length=1, max_length=64)
    credential: dict[str, Any]
