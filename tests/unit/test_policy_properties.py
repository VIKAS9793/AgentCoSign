"""Properties that must hold for every input, not just the tabled ones.

Hypothesis explores the input space. Two properties carry the product claim:
an automatic ALLOW is never unsafe, and no sequence of proposals can push
exposure past the captured amount or the daily cap, even if every escalation is
later approved.
"""

from datetime import UTC, datetime, timedelta
from decimal import Decimal

from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

from agentcosign.models.domain import Decision
from agentcosign.models.policy import (
    LedgerContext,
    OrderEvidence,
    Proposal,
    ScreeningVerdict,
    Signals,
)
from agentcosign.services.policy_engine import evaluate
from tests.scenarios.policy_scenarios import ACCOUNT, NOW, policy, screening

RULES = policy()
SCREEN = screening()
CAPTURED = Decimal("100.00")

cents = st.integers(min_value=-500, max_value=300_000).map(
    lambda n: Decimal(n) / Decimal(100)
)
odd_decimals = st.sampled_from(
    [Decimal("NaN"), Decimal("Infinity"), Decimal("-Infinity"), Decimal("1E+30")]
)
amounts = st.one_of(cents, odd_decimals)
verdicts = st.sampled_from(list(ScreeningVerdict))

COMMON = settings(
    max_examples=300,
    deadline=None,
    suppress_health_check=[HealthCheck.too_slow],
)


def _evidence(refunded: Decimal, disputed: bool, age_seconds: int) -> OrderEvidence:
    return OrderEvidence(
        order_id="ORD-1",
        currency="USD",
        captured_amount=CAPTURED,
        refunded_amount=refunded,
        account_id=ACCOUNT,
        has_open_dispute=disputed,
        fetched_at=NOW - timedelta(seconds=age_seconds),
        dispute_id="DSP-1" if disputed else None,
        dispute_amount=Decimal("10.00") if disputed else None,
    )


@COMMON
@given(
    amount=amounts,
    refunded=st.integers(0, 10_000).map(lambda n: Decimal(n) / 100),
    reserved=st.integers(0, 10_000).map(lambda n: Decimal(n) / 100),
    outflow=st.integers(0, 200_000).map(lambda n: Decimal(n) / 100),
    refunds_today=st.integers(0, 8),
    disputed=st.booleans(),
    age=st.integers(-30, 200),
    screen=verdicts,
    prescreen=st.booleans(),
    currency=st.sampled_from(["USD", "EUR", "usd", ""]),
)
def test_an_allow_is_never_unsafe(
    amount: Decimal,
    refunded: Decimal,
    reserved: Decimal,
    outflow: Decimal,
    refunds_today: int,
    disputed: bool,
    age: int,
    screen: ScreeningVerdict,
    prescreen: bool,
    currency: str,
) -> None:
    proposal = Proposal(
        request_id="req",
        tool="create_refund",
        amount=amount,
        currency=currency,
        order_id="ORD-1",
    )
    ledger = LedgerContext(
        expected_account_id=ACCOUNT,
        reserved_for_order=reserved,
        refunds_24h_for_customer=refunds_today,
        outflow_24h=outflow,
    )
    result = evaluate(
        proposal,
        _evidence(refunded, disputed, age),
        ledger,
        Signals(screening=screen, prescreen_hit=prescreen),
        RULES,
        SCREEN,
        NOW,
    )
    assert result.decision in set(Decision)
    if result.decision is not Decision.ALLOW:
        return
    assert Decimal("0") < amount <= RULES.refund_auto_limit
    assert refunded + reserved + amount <= CAPTURED
    assert outflow + amount <= RULES.daily_outflow_cap
    assert refunds_today + 1 <= RULES.velocity_max_refunds_24h
    assert not disputed
    assert screen is ScreeningVerdict.CLEAN
    assert not prescreen
    assert currency == "USD"
    assert 0 <= age <= RULES.evidence_max_age_seconds


@COMMON
@given(
    steps=st.lists(
        st.tuples(
            st.integers(0, 2), st.integers(1, 12_000).map(lambda n: Decimal(n) / 100)
        ),
        min_size=1,
        max_size=60,
    )
)
def test_exposure_stays_bounded_even_if_every_escalation_is_approved(
    steps: list[tuple[int, Decimal]],
) -> None:
    refunded = {0: Decimal("0"), 1: Decimal("0"), 2: Decimal("0")}
    customer_count = 0
    outflow = Decimal("0")
    auto_total = Decimal("0")
    for order, amount in steps:
        evidence = replace_order(_evidence(refunded[order], False, 5), f"ORD-{order}")
        proposal = Proposal(
            request_id=f"req-{order}",
            tool="create_refund",
            amount=amount,
            currency="USD",
            order_id=f"ORD-{order}",
        )
        ledger = LedgerContext(
            expected_account_id=ACCOUNT,
            refunds_24h_for_customer=customer_count,
            outflow_24h=outflow,
        )
        result = evaluate(proposal, evidence, ledger, Signals(), RULES, SCREEN, NOW)
        if result.decision is Decision.DENY:
            continue
        refunded[order] += amount  # worst case: the escalation is approved
        outflow += amount
        customer_count += 1
        if result.decision is Decision.ALLOW:
            auto_total += amount
    assert all(total <= CAPTURED for total in refunded.values())
    assert outflow <= RULES.daily_outflow_cap
    assert auto_total <= RULES.daily_outflow_cap


def replace_order(evidence: OrderEvidence, order_id: str) -> OrderEvidence:
    """Copy evidence under a different order id."""
    return OrderEvidence(
        order_id=order_id,
        currency=evidence.currency,
        captured_amount=evidence.captured_amount,
        refunded_amount=evidence.refunded_amount,
        account_id=evidence.account_id,
        has_open_dispute=evidence.has_open_dispute,
        fetched_at=evidence.fetched_at,
        dispute_id=evidence.dispute_id,
        dispute_amount=evidence.dispute_amount,
    )


@COMMON
@given(
    tool=st.text(max_size=30),
    amount=amounts,
    currency=st.text(max_size=5),
    order=st.one_of(st.none(), st.text(max_size=12)),
)
def test_arbitrary_input_never_raises_and_unknown_tools_never_pass(
    tool: str, amount: Decimal, currency: str, order: str | None
) -> None:
    proposal = Proposal(
        request_id="req", tool=tool, amount=amount, currency=currency, order_id=order
    )
    result = evaluate(
        proposal,
        _evidence(Decimal("0"), False, 1),
        LedgerContext(expected_account_id=ACCOUNT),
        Signals(),
        RULES,
        SCREEN,
        datetime(2026, 11, 2, 12, 0, tzinfo=UTC),
    )
    if tool not in {"create_refund", "accept_dispute_claim", "payout"}:
        assert result.decision is Decision.DENY
        assert result.rule_id == "R12"
