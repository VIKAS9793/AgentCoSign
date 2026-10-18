"""End to end below the API: evidence, decision, reservation, execution, audit."""

from dataclasses import replace
from decimal import Decimal
from pathlib import Path

import pytest

from agentcosign.clients.paypal import RecordedPayPalClient
from agentcosign.core.exceptions import PayPalClientError
from agentcosign.models.domain import Decision
from agentcosign.models.policy import OrderEvidence, Proposal, Signals
from agentcosign.services.evidence import fetch_evidence
from agentcosign.services.executor import execute
from agentcosign.services.ledger import Ledger, ProposalState
from tests.scenarios.policy_scenarios import NOW, policy, screening
from tests.unit.paypal_fixtures import ACCOUNT, dispute_json, order_json

SECRET = b"s" * 32
WINDOW = 600  # seconds allowed to decide


def refund(request_id: str, amount: str) -> Proposal:
    return Proposal(
        request_id=request_id,
        tool="create_refund",
        amount=Decimal(amount),
        currency="USD",
        order_id="ORD-1",
    )


@pytest.fixture
def ledger(tmp_path: Path) -> Ledger:
    return Ledger(tmp_path / "ledger.db", SECRET, policy(), ACCOUNT)


async def decide(
    ledger: Ledger, client: RecordedPayPalClient, proposal: Proposal
) -> OrderEvidence:
    ev = await fetch_evidence(client, "ORD-1", now=NOW, secret=SECRET)
    ledger.decide(proposal, ev, Signals(), screening(), NOW)
    return ev


async def test_allowed_refund_executes_and_paypal_sees_it(ledger: Ledger) -> None:
    client = RecordedPayPalClient({"ORD-1": order_json()})
    decided = await decide(ledger, client, refund("r1", "10.00"))
    outcome = await execute(
        refund("r1", "10.00"), decided, client, ledger, now=NOW, secret=SECRET
    )
    assert outcome.state is ProposalState.EXECUTED
    assert outcome.paypal_id and outcome.paypal_id.startswith("RF-")
    after = await fetch_evidence(client, "ORD-1", now=NOW, secret=SECRET)
    assert after.refunded_amount == Decimal("10.00")
    assert ledger.verify_chain() == 3  # decided, execution attempt, executed
    assert ledger.verify_records() == 1


async def test_executed_refunds_count_through_paypal_not_twice(ledger: Ledger) -> None:
    client = RecordedPayPalClient({"ORD-1": order_json(captured="30.00")})
    decided = await decide(ledger, client, refund("r1", "20.00"))
    await execute(
        refund("r1", "20.00"), decided, client, ledger, now=NOW, secret=SECRET
    )
    ev = await fetch_evidence(client, "ORD-1", now=NOW, secret=SECRET)
    ok = ledger.decide(refund("r2", "10.00"), ev, Signals(), screening(), NOW)
    over = ledger.decide(refund("r3", "10.01"), ev, Signals(), screening(), NOW)
    # exactly what is left fits (a second automatic refund on the order now asks
    # the owner, but never because the first was counted twice)
    assert "R4" not in ok.result.triggered_rules
    assert ok.result.decision is not Decision.DENY
    assert over.result.rule_id == "R4"


@pytest.mark.edge
async def test_a_dispute_opened_before_execution_sends_it_back_for_approval(
    ledger: Ledger,
) -> None:
    client = RecordedPayPalClient({"ORD-1": order_json()})
    decided = await decide(ledger, client, refund("r1", "10.00"))
    client._disputes["CAP-1"] = [dispute_json()]  # noqa: SLF001 -- simulate PayPal changing
    outcome = await execute(
        refund("r1", "10.00"), decided, client, ledger, now=NOW, secret=SECRET
    )
    assert outcome.state is ProposalState.PENDING_APPROVAL
    assert outcome.reason == "state_drift"
    assert (
        await fetch_evidence(client, "ORD-1", now=NOW, secret=SECRET)
    ).refunded_amount == 0
    # the owner is asked again with the dispute named, and the record is sealed
    waiting = next(i for i in ledger.pending_approvals() if i["request_id"] == "r1")
    assert "R6" in waiting["triggered"]
    ledger.verify_request("r1")
    ledger.verify_records()


@pytest.mark.edge
async def test_a_changed_order_stops_the_payment(ledger: Ledger) -> None:
    client = RecordedPayPalClient({"ORD-1": order_json()})
    decided = await decide(ledger, client, refund("r1", "10.00"))
    stale = replace(decided, captured_amount=Decimal("200.00"))
    outcome = await execute(
        refund("r1", "10.00"), stale, client, ledger, now=NOW, secret=SECRET
    )
    assert outcome.state is ProposalState.FAILED
    assert ledger.state_of("r1") is ProposalState.FAILED


class RefusingClient(RecordedPayPalClient):
    def __init__(self, retryable: bool) -> None:
        super().__init__({"ORD-1": order_json()})
        self.retryable = retryable

    async def refund_capture(self, *args: object, **kwargs: object) -> dict:  # type: ignore[override] -- test double
        raise PayPalClientError("refused", {"status": 422, "retryable": self.retryable})


async def test_a_definite_refusal_releases_the_reservation(ledger: Ledger) -> None:
    client = RefusingClient(retryable=False)
    decided = await decide(ledger, client, refund("r1", "60.00"))
    # approved
    ledger.transition("r1", ProposalState.EXECUTING, NOW, decide_within=WINDOW)
    outcome = await execute(
        refund("r1", "60.00"), decided, client, ledger, now=NOW, secret=SECRET
    )
    assert outcome.state is ProposalState.FAILED
    again = ledger.decide(refund("r2", "60.00"), decided, Signals(), screening(), NOW)
    assert again.result.decision is Decision.ESCALATE  # the 60.00 is free again


@pytest.mark.edge
async def test_an_unknown_outcome_keeps_the_reservation_for_a_safe_retry(
    ledger: Ledger,
) -> None:
    client = RefusingClient(retryable=True)
    decided = await decide(ledger, client, refund("r1", "60.00"))
    ledger.transition("r1", ProposalState.EXECUTING, NOW, decide_within=WINDOW)
    outcome = await execute(
        refund("r1", "60.00"), decided, client, ledger, now=NOW, secret=SECRET
    )
    assert outcome.state is ProposalState.EXECUTING
    assert outcome.reason == "retry_with_same_id"
    blocked = ledger.decide(refund("r2", "60.00"), decided, Signals(), screening(), NOW)
    assert blocked.result.rule_id == "R4"  # still reserved


async def test_dispute_concession_executes_after_approval(ledger: Ledger) -> None:
    client = RecordedPayPalClient(
        {"ORD-1": order_json(captured="500.00")}, {"CAP-1": [dispute_json()]}
    )
    claim = Proposal(
        request_id="d1",
        tool="accept_dispute_claim",
        amount=Decimal("40.00"),
        currency="USD",
        order_id="ORD-1",
        dispute_id="DSP-1",
    )
    decided = await decide(ledger, client, claim)
    assert ledger.state_of("d1") is ProposalState.PENDING_APPROVAL
    ledger.transition(
        "d1",
        ProposalState.EXECUTING,
        NOW,
        {"assurance": "passkey"},
        decide_within=WINDOW,
    )
    outcome = await execute(claim, decided, client, ledger, now=NOW, secret=SECRET)
    assert outcome.state is ProposalState.EXECUTED
