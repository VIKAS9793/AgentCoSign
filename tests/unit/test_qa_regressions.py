"""Regression tests for defects found by the QA review, one test per finding."""

import sqlite3
from decimal import Decimal
from pathlib import Path

import pytest

from agentcosign.clients.paypal import RecordedPayPalClient
from agentcosign.core.exceptions import AuditIntegrityError, PayPalClientError
from agentcosign.models.domain import Decision
from agentcosign.models.policy import Proposal, Signals
from agentcosign.services.evidence import fetch_evidence, order_evidence_from
from agentcosign.services.executor import execute
from agentcosign.services.ledger import Ledger, ProposalState
from tests.scenarios.policy_scenarios import NOW, policy, screening
from tests.unit.paypal_fixtures import ACCOUNT, dispute_json, order_json

SECRET = b"s" * 32


def refund(request_id: str, amount: str, order: str = "ORD-1") -> Proposal:
    return Proposal(
        request_id=request_id,
        tool="create_refund",
        amount=Decimal(amount),
        currency="USD",
        order_id=order,
    )


@pytest.fixture
def ledger(tmp_path: Path) -> Ledger:
    return Ledger(tmp_path / "ledger.db", SECRET, policy(), ACCOUNT)


async def _decided(ledger: Ledger, client: RecordedPayPalClient, proposal: Proposal):  # noqa: ANN202
    ev = await fetch_evidence(client, proposal.order_id or "", now=NOW, secret=SECRET)
    ledger.decide(proposal, ev, Signals(), screening(), NOW)
    return ev


async def _refunded(client: RecordedPayPalClient) -> Decimal:
    return (
        await fetch_evidence(client, "ORD-1", now=NOW, secret=SECRET)
    ).refunded_amount


# --- the executor must not trust its caller
async def test_a_proposal_awaiting_approval_cannot_be_executed(ledger: Ledger) -> None:
    client = RecordedPayPalClient({"ORD-1": order_json()})
    decided = await _decided(ledger, client, refund("r1", "60.00"))  # escalated
    outcome = await execute(
        refund("r1", "60.00"), decided, client, ledger, now=NOW, secret=SECRET
    )
    assert outcome.reason == "not_executable"
    assert await _refunded(client) == 0  # no money moved
    assert ledger.state_of("r1") is ProposalState.PENDING_APPROVAL


async def test_execution_cannot_change_the_decided_amount(ledger: Ledger) -> None:
    client = RecordedPayPalClient({"ORD-1": order_json()})
    decided = await _decided(ledger, client, refund("r1", "10.00"))  # allowed
    tampered = refund("r1", "90.00")
    outcome = await execute(tampered, decided, client, ledger, now=NOW, secret=SECRET)
    assert outcome.reason == "not_executable"
    assert await _refunded(client) == 0


async def test_a_denied_proposal_cannot_be_executed(ledger: Ledger) -> None:
    client = RecordedPayPalClient({"ORD-1": order_json()})
    decided = await _decided(ledger, client, refund("r1", "300.00"))  # denied
    outcome = await execute(
        refund("r1", "300.00"), decided, client, ledger, now=NOW, secret=SECRET
    )
    assert outcome.reason == "not_executable"
    assert await _refunded(client) == 0


# --- a retry after a lost reply must finish the same refund, not re-ask
class LosesFirstReply(RecordedPayPalClient):
    """PayPal applies the refund but the first reply never arrives."""

    def __init__(self) -> None:
        super().__init__({"ORD-1": order_json()})
        self.calls = 0

    async def refund_capture(self, capture_id, amount, currency, request_id):  # type: ignore[no-untyped-def,override] -- test double
        self.calls += 1
        result = await super().refund_capture(capture_id, amount, currency, request_id)
        if self.calls == 1:
            raise PayPalClientError("timed out", {"retryable": True})
        return result


async def test_retry_after_a_lost_reply_completes_without_a_second_refund(
    ledger: Ledger,
) -> None:
    client = LosesFirstReply()
    decided = await _decided(ledger, client, refund("r1", "20.00"))
    first = await execute(
        refund("r1", "20.00"), decided, client, ledger, now=NOW, secret=SECRET
    )
    assert first.reason == "retry_with_same_id"
    second = await execute(
        refund("r1", "20.00"), decided, client, ledger, now=NOW, secret=SECRET
    )
    assert second.state is ProposalState.EXECUTED
    assert await _refunded(client) == Decimal("20.00")  # once, not twice
    assert ledger.verify_chain() >= 4


# --- the cumulative cap covers dispute concessions too
async def test_refund_and_concession_together_cannot_exceed_captured(
    ledger: Ledger,
) -> None:
    client = RecordedPayPalClient(
        {"ORD-1": order_json()}, {"CAP-1": [dispute_json(amount="60.00")]}
    )
    ev = await fetch_evidence(client, "ORD-1", now=NOW, secret=SECRET)
    ledger.decide(refund("r1", "60.00"), ev, Signals(), screening(), NOW)  # escalated
    claim = Proposal(
        request_id="d1",
        tool="accept_dispute_claim",
        amount=Decimal("60.00"),
        currency="USD",
        order_id="ORD-1",
        dispute_id="DSP-1",
    )
    recorded = ledger.decide(claim, ev, Signals(), screening(), NOW)
    assert recorded.result.decision is Decision.DENY
    assert recorded.result.rule_id == "R4"


# --- evidence must not read an open dispute as none
def test_an_open_dispute_without_an_id_still_counts_as_open() -> None:
    nameless = {
        "status": "OPEN",
        "dispute_amount": {"value": "40.00", "currency_code": "USD"},
    }
    ev = order_evidence_from(order_json(), [nameless], fetched_at=NOW, secret=SECRET)
    assert ev.has_open_dispute is True
    assert ev.dispute_id is None


# --- the per-customer limit must work for wallet payments
def test_wallet_payments_get_a_customer_key_from_the_payer_id() -> None:
    wallet = order_json(card=False)
    wallet["payer"] = {"payer_id": "PAYERID123"}
    ev = order_evidence_from(wallet, [], fetched_at=NOW, secret=SECRET)
    assert ev.customer_key is not None
    assert ev.customer_key.startswith("payer:")
    assert "PAYERID123" not in ev.customer_key


async def test_velocity_fires_for_repeat_wallet_refunds(ledger: Ledger) -> None:
    decisions = []
    for index in range(4):
        order = f"ORD-{index}"
        body = order_json(order=order, capture=f"CAP-{index}", card=False)
        body["payer"] = {"payer_id": "PAYERID123"}
        ev = order_evidence_from(body, [], fetched_at=NOW, secret=SECRET)
        recorded = ledger.decide(
            refund(f"r{index}", "5.00", order), ev, Signals(), screening(), NOW
        )
        decisions.append(recorded.result.decision)
    assert decisions[:3] == [Decision.ALLOW] * 3
    assert decisions[3] is Decision.ESCALATE


# --- edits to the proposals table must be detected
async def test_editing_a_recorded_decision_is_detected(
    ledger: Ledger, tmp_path: Path
) -> None:
    client = RecordedPayPalClient({"ORD-1": order_json()})
    await _decided(ledger, client, refund("r1", "300.00"))  # denied
    assert ledger.verify_records() == 1
    with sqlite3.connect(tmp_path / "ledger.db", isolation_level=None) as conn:
        conn.execute("UPDATE proposals SET state = 'executing', decision = 'allow'")
    with pytest.raises(AuditIntegrityError):
        ledger.verify_records()
