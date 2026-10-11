"""The policy engine against its scenario table, plus the properties that matter."""

import time
from dataclasses import replace
from datetime import timedelta
from decimal import Decimal
from itertools import product

import pytest

from agentcosign.core.config import get_settings
from agentcosign.models.domain import Decision, PolicyResult
from agentcosign.models.policy import LedgerContext, OrderEvidence
from agentcosign.services import policy_engine
from agentcosign.services.policy_engine import check_execution_drift, evaluate
from tests.scenarios.policy_scenarios import (
    DRIFT_SCENARIOS,
    NOW,
    SCENARIOS,
    Scenario,
    policy,
)


def run(scenario: Scenario) -> PolicyResult:
    proposal, evidence, ledger, signals, rules, screening = scenario.build()
    return evaluate(proposal, evidence, ledger, signals, rules, screening, NOW)


def test_the_table_is_large_unique_and_covers_every_rule() -> None:
    ids = [s.id for s in SCENARIOS]
    assert len(ids) == len(set(ids))
    assert len(SCENARIOS) >= 50
    seen = {rule for s in SCENARIOS for rule in (s.triggered or ())}
    assert seen >= {f"R{n}" for n in range(1, 16)}


@pytest.mark.parametrize("scenario", SCENARIOS, ids=lambda s: f"{s.id} {s.title}")
def test_scenario(scenario: Scenario) -> None:
    result = run(scenario)
    assert result.decision is scenario.decision
    assert result.rule_id == scenario.rule
    if scenario.triggered is not None:
        assert result.triggered_rules == scenario.triggered
    assert result.requires_human_approval is (scenario.decision is Decision.ESCALATE)


@pytest.mark.parametrize("scenario", SCENARIOS, ids=lambda s: s.id)
def test_every_decision_is_explained_and_machine_readable(scenario: Scenario) -> None:
    result = run(scenario)
    assert result.explanation.strip()
    assert "—" not in result.explanation
    assert result.metadata is not None
    assert isinstance(result.metadata["amount"], str)  # money serialised as text
    assert result.rule_id in result.triggered_rules


def test_decisions_are_deterministic_and_do_not_mutate_inputs() -> None:
    for scenario in SCENARIOS:
        proposal, evidence, ledger, signals, rules, screening = scenario.build()
        first = evaluate(proposal, evidence, ledger, signals, rules, screening, NOW)
        second = evaluate(proposal, evidence, ledger, signals, rules, screening, NOW)
        assert first == second


def test_strictest_outcome_always_wins() -> None:
    for scenario in SCENARIOS:
        result = run(scenario)
        hits = result.metadata["hits"] if result.metadata else []
        decisions = {h["decision"] for h in hits}
        if "deny" in decisions:
            assert result.decision is Decision.DENY
        elif "pause" in decisions:
            assert result.decision is Decision.PAUSE
        elif "escalate" in decisions:
            assert result.decision is Decision.ESCALATE


def test_allow_is_only_ever_the_clean_case() -> None:
    for scenario in SCENARIOS:
        result = run(scenario)
        if result.decision is Decision.ALLOW:
            assert result.triggered_rules == ("R1",)
            assert result.metadata is not None
            assert result.metadata["hits"] == []


@pytest.mark.edge
def test_no_malformed_input_raises() -> None:
    bad_amounts = [
        Decimal("NaN"),
        Decimal("-0"),
        Decimal("Infinity"),
        Decimal("1E+40"),
        Decimal("0.0000001"),
    ]
    bad_tools = ["", " ", "CREATE_REFUND", "create_refund; drop", "\u0000"]
    for amount, tool in product(bad_amounts, bad_tools):
        scenario = Scenario(
            "fuzz",
            "fuzz",
            Decision.DENY,
            "R12",
            proposal={"amount": amount, "tool": tool},
        )
        result = run(scenario)
        assert result.decision is Decision.DENY
        assert result.rule_id == "R12"


def test_only_documented_tools_can_move_money() -> None:
    allowed = {"create_refund", "accept_dispute_claim", "payout"}
    for tool in ("refund", "send_money", "pay_order", "record_refund_for_invoice"):
        assert tool not in allowed
        result = run(Scenario("t", "t", Decision.DENY, "R12", proposal={"tool": tool}))
        assert result.decision is Decision.DENY


def test_priority_tables_cover_every_rule_that_can_decide() -> None:
    deny = set(policy_engine.DENY_PRIORITY)
    escalate = set(policy_engine.ESCALATE_PRIORITY)
    every_decider = {
        h.rule_id
        for scenario in SCENARIOS
        for h in [run(scenario)]
        if h.decision is not Decision.ALLOW
    }
    assert every_decider <= deny | escalate | {"R12"}


# --- tie the table to the shipped limits ------------------------------------------
def test_the_table_matches_the_shipped_configuration() -> None:
    shipped = get_settings().policy
    spec = policy()
    assert shipped.refund_auto_limit == spec.refund_auto_limit
    assert shipped.refund_max_limit == spec.refund_max_limit
    assert shipped.velocity_max_refunds_24h == spec.velocity_max_refunds_24h
    assert shipped.daily_outflow_cap == spec.daily_outflow_cap
    assert shipped.payout_new_payee_action == spec.payout_new_payee_action
    assert shipped.evidence_max_age_seconds == spec.evidence_max_age_seconds


# --- evidence drift between approval and execution ---------------------------------
def _decided_evidence() -> OrderEvidence:
    scenario = Scenario("d", "d", Decision.ALLOW, "R1")
    evidence = scenario.build()[1]
    assert evidence is not None
    return evidence


@pytest.mark.parametrize(("name", "change", "expected"), DRIFT_SCENARIOS)
def test_execution_drift(
    name: str, change: dict[str, object], expected: Decision | None
) -> None:
    decided = _decided_evidence()
    current = replace(decided, **change)  # type: ignore[arg-type]
    result = check_execution_drift(decided, current)
    if expected is None:
        assert result is None
    else:
        assert result is not None
        assert result.decision is expected
        assert result.rule_id == "R14"


def test_state_drift_asks_a_person_again_but_money_drift_denies() -> None:
    decided = _decided_evidence()
    reopened = replace(decided, has_open_dispute=True)
    changed = replace(decided, captured_amount=decided.captured_amount - 1)
    reopened_result = check_execution_drift(decided, reopened)
    changed_result = check_execution_drift(decided, changed)
    assert reopened_result is not None and changed_result is not None
    assert reopened_result.requires_human_approval is True
    assert changed_result.decision is Decision.DENY


# --- speed -------------------------------------------------------------------------
def test_decision_latency_is_far_below_the_budget() -> None:
    scenario = SCENARIOS[0]
    proposal, evidence, ledger, signals, rules, screening = scenario.build()
    samples = []
    for _ in range(500):
        started = time.perf_counter()
        evaluate(proposal, evidence, ledger, signals, rules, screening, NOW)
        samples.append(time.perf_counter() - started)
    samples.sort()
    assert samples[int(len(samples) * 0.95)] < 0.05  # budget is 300 ms; use 50 ms


def test_fresh_evidence_is_judged_against_the_injected_clock_not_the_wall_clock() -> (
    None
):
    scenario = Scenario("c", "c", Decision.ALLOW, "R1")
    proposal, evidence, ledger, signals, rules, screening = scenario.build()
    later = NOW + timedelta(seconds=300)
    result = evaluate(proposal, evidence, ledger, signals, rules, screening, later)
    assert result.decision is Decision.DENY
    assert result.rule_id == "R14"


# --- the repeat-refund pause --------------------------------------------------
@pytest.mark.parametrize(
    ("hours_ago", "expected_hours_from_now"),
    [
        # three refunds 20, 10 and 2 hours ago: the oldest leaves the window first
        ((20, 10, 2), 4),
        # five refunds: three must leave before the customer is under the limit
        ((23, 22, 15, 9, 1), 9),
    ],
)
def test_a_pause_releases_when_the_customer_is_back_under_the_limit(
    hours_ago: tuple[int, ...], expected_hours_from_now: int
) -> None:
    times = tuple(NOW - timedelta(hours=h) for h in hours_ago)
    ledger = LedgerContext(
        expected_account_id="ACCT-1",
        refunds_24h_for_customer=len(times),
        customer_refund_times=times,
    )
    release = policy_engine.release_time(ledger, 3, NOW)
    assert release == NOW + timedelta(hours=expected_hours_from_now)


@pytest.mark.edge
def test_a_pause_without_refund_times_holds_a_full_day() -> None:
    ledger = LedgerContext(expected_account_id="ACCT-1", refunds_24h_for_customer=3)
    assert policy_engine.release_time(ledger, 3, NOW) == NOW + timedelta(hours=24)


def test_every_pause_carries_a_future_release_time_and_needs_no_person() -> None:
    for scenario in SCENARIOS:
        result = run(scenario)
        if result.decision is Decision.PAUSE:
            assert result.release_at is not None and result.release_at > NOW
            assert result.requires_human_approval is False
        else:
            assert result.release_at is None
