"""What the merchant is told must match what the engine decided.

An approval only means something if the person saw every reason the request
needed them and the true state of the safety check. These tests run the whole
policy scenario table through the approver view, so a change to the engine, the
escalation order or the wording cannot silently hide a reason or misstate a fact.
"""

import re
from dataclasses import replace

import pytest
from hypothesis import given
from hypothesis import strategies as st

from agentcosign.core.config import PolicySettings
from agentcosign.models.domain import Decision, PolicyResult
from agentcosign.services.approval_view import (
    ONLY_SAFETY_REASON,
    SAFETY_PASSED,
    SAFETY_UNAVAILABLE,
    business_reasons,
    plain_reason,
    safety_check,
)
from agentcosign.services.policy_engine import (
    ESCALATE_PRIORITY,
    RULE_EVIDENCE,
    RULE_SCREENING_DOWN,
    RULE_VELOCITY,
    evaluate,
)
from tests.scenarios.policy_scenarios import NOW, SCENARIOS, Scenario, policy

BUSINESS_RULES = tuple(r for r in ESCALATE_PRIORITY if r != RULE_SCREENING_DOWN) + (
    RULE_EVIDENCE,
)
ESCALATIONS = [s for s in SCENARIOS if s.decision is Decision.ESCALATE]


def _engine_result(scenario: Scenario) -> tuple[PolicyResult, PolicySettings]:
    proposal, evidence, ledger, signals, rules, screening = scenario.build()
    return evaluate(proposal, evidence, ledger, signals, rules, screening, NOW), rules


def test_the_table_has_escalations_to_check() -> None:
    assert len(ESCALATIONS) >= 10


@pytest.mark.parametrize("scenario", ESCALATIONS, ids=lambda s: s.id)
def test_every_business_reason_the_engine_found_is_shown(scenario: Scenario) -> None:
    result, rules = _engine_result(scenario)
    shown = business_reasons(result.triggered_rules, rules)
    for rule in result.triggered_rules:
        if rule in BUSINESS_RULES:
            assert plain_reason(rule, rules) in shown, (scenario.id, rule)


@pytest.mark.parametrize("scenario", ESCALATIONS, ids=lambda s: s.id)
def test_the_safety_status_matches_the_engine(scenario: Scenario) -> None:
    result, _ = _engine_result(scenario)
    expected = (
        SAFETY_UNAVAILABLE
        if RULE_SCREENING_DOWN in result.triggered_rules
        else SAFETY_PASSED
    )
    assert safety_check(result.triggered_rules) == expected


@given(st.sets(st.sampled_from(BUSINESS_RULES + (RULE_SCREENING_DOWN,)), min_size=1))
def test_no_combination_of_rules_hides_a_reason(triggered: set[str]) -> None:
    rules = policy()
    shown = business_reasons(sorted(triggered), rules)
    business = [r for r in triggered if r in BUSINESS_RULES]
    assert len(shown) == max(len(business), 1)
    assert len(set(shown)) == len(shown)
    if not business:
        assert shown == [ONLY_SAFETY_REASON]
    for line in shown:
        assert not re.search(r"\bR\d+\b", line)
    if RULE_SCREENING_DOWN in triggered:
        assert safety_check(sorted(triggered)) == SAFETY_UNAVAILABLE


@pytest.mark.parametrize("prior", [3, 4, 5, 9])
def test_the_repeat_refund_line_is_true_for_every_count_that_fires_it(
    prior: int,
) -> None:
    # find a clean refund scenario and vary only the customer's earlier refunds
    base = next(s for s in SCENARIOS if s.decision is Decision.ALLOW)
    proposal, evidence, ledger, signals, rules, screening = base.build()
    ledger = replace(ledger, refunds_24h_for_customer=prior)
    result = evaluate(proposal, evidence, ledger, signals, rules, screening, NOW)
    assert RULE_VELOCITY in result.triggered_rules
    line = plain_reason(RULE_VELOCITY, rules)
    limit = rules.velocity_max_refunds_24h
    # the claim is "has reached your limit": true exactly when prior >= limit
    assert prior >= limit
    assert f"reached your limit of {limit}" in line
    assert "more than" not in line
