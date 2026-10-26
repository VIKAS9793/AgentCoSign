"""Deterministic policy engine: the only component that authorises money movement.

A pure function of its inputs: no network, database, clock or randomness (the
caller supplies ``now``). Models propose and explain; this code decides.

Outcomes are ordered DENY > ESCALATE > ALLOW and the strictest one wins. The
result names the deciding rule and every rule that fired, so the ledger and the
approver see the whole picture. Anything malformed, missing or inconsistent is a
DENY under the fail-closed rule; it never raises for bad data.

Adding a rule means adding one function and one registry entry. Existing rule
functions are not edited to change behaviour of another rule.
"""

import re
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timedelta
from decimal import Decimal, InvalidOperation

from agentcosign.core.config import PolicySettings, ScreeningSettings
from agentcosign.models.domain import Decision, PolicyResult, ToolAction
from agentcosign.models.policy import (
    REQUEST_ID_PATTERN,
    CustomerReading,
    LedgerContext,
    OrderEvidence,
    Proposal,
    ReadingStatus,
    ScreeningVerdict,
    Signals,
)

CENT = Decimal("0.01")
MAX_AMOUNT = Decimal("1000000000")
CURRENCY_PATTERN = re.compile(r"^[A-Z]{3}$")

# Rule identifiers as recorded in the ledger and shown to approvers.
RULE_ALLOW = "R1"
RULE_ABOVE_AUTO = "R2"
RULE_ABOVE_MAX = "R3"
RULE_CUMULATIVE = "R4"
RULE_VELOCITY = "R5"
RULE_OPEN_DISPUTE = "R6"
RULE_DISPUTE_CONCESSION = "R7"
RULE_NEW_PAYEE = "R8"
RULE_DUPLICATE_PAYOUT = "R9"
RULE_DAILY_CAP = "R10"
RULE_INJECTION = "R11"
RULE_INVALID = "R12"
RULE_MISMATCH_SCOPE = "R13"
RULE_EVIDENCE = "R14"
RULE_SCREENING_DOWN = "R15"
RULE_STOPPED = "R16"
RULE_AUTO_BUDGET = "R17"
RULE_CUSTOMER_WORDS = "R18"

VELOCITY_WINDOW = timedelta(hours=24)
# never release in the past or in the same instant, so a pause always holds
MIN_PAUSE = timedelta(minutes=1)

# When several rules of the same strictness fire, the first one listed decides.
# Safety invariants come first so the reason shown is the most fundamental one.
DENY_PRIORITY = (
    RULE_INVALID,
    RULE_STOPPED,
    RULE_MISMATCH_SCOPE,
    RULE_EVIDENCE,
    RULE_INJECTION,
    RULE_CUMULATIVE,
    RULE_DUPLICATE_PAYOUT,
    RULE_DAILY_CAP,
    RULE_ABOVE_MAX,
    RULE_SCREENING_DOWN,
)
ESCALATE_PRIORITY = (
    RULE_DISPUTE_CONCESSION,
    RULE_OPEN_DISPUTE,
    RULE_CUSTOMER_WORDS,
    RULE_NEW_PAYEE,
    RULE_SCREENING_DOWN,
    RULE_VELOCITY,
    RULE_AUTO_BUDGET,
    RULE_ABOVE_AUTO,
)

_RULE_TITLES = {
    RULE_ALLOW: "Within the automatic limit with clean evidence",
    RULE_ABOVE_AUTO: "Above the automatic limit",
    RULE_ABOVE_MAX: "Above the maximum limit",
    RULE_CUMULATIVE: "Refunds would exceed what was captured",
    RULE_VELOCITY: "Too many refunds for this customer today",
    RULE_OPEN_DISPUTE: "The order has an open dispute",
    RULE_DISPUTE_CONCESSION: "Accepting a dispute concedes funds",
    RULE_NEW_PAYEE: "New or changed payee",
    RULE_DUPLICATE_PAYOUT: "Duplicate payout",
    RULE_DAILY_CAP: "Daily outflow cap reached",
    RULE_INJECTION: "Request looks like an injection attempt",
    RULE_INVALID: "Request is invalid or incomplete",
    RULE_MISMATCH_SCOPE: "Currency or account does not match",
    RULE_EVIDENCE: "Claims do not match PayPal's records",
    RULE_SCREENING_DOWN: "Screening is unavailable",
    RULE_STOPPED: "The merchant stopped all refunds",
    RULE_AUTO_BUDGET: "Automatic refunds on this order or customer today are used up",
    RULE_CUSTOMER_WORDS: "The customer's own words do not support sending this alone",
}


def rule_title(rule_id: str) -> str:
    """What a rule means, in words, for anyone reading a decision."""
    return _RULE_TITLES.get(rule_id, "Unknown check")


# Reasons whose truth PayPal's record for the order cannot show: a second charge
# elsewhere, an earlier refund, a charge the cardholder disowns, a return.
UNPROVABLE_REASONS = frozenset(
    {"duplicate_charge", "refund_not_received", "unrecognised_charge", "changed_mind"}
)


@dataclass(frozen=True, slots=True)
class RuleHit:
    """One rule that fired, with a machine code and a plain-language detail."""

    rule_id: str
    decision: Decision
    code: str
    detail: str


@dataclass(frozen=True, slots=True)
class _Case:
    proposal: Proposal
    evidence: OrderEvidence | None
    ledger: LedgerContext
    signals: Signals
    policy: PolicySettings
    screening: ScreeningSettings
    now: datetime

    @property
    def tool(self) -> ToolAction:
        """The proposal's tool; only called after validation has accepted it."""
        return ToolAction(self.proposal.tool)

    @property
    def moves_by_amount(self) -> bool:
        """True for tools that are bounded by the automatic and maximum limits."""
        return self.tool in (ToolAction.CREATE_REFUND, ToolAction.PAYOUT)


Rule = Callable[[_Case], RuleHit | None]


def _hit(rule_id: str, decision: Decision, code: str, detail: str) -> RuleHit:
    return RuleHit(rule_id, decision, code, detail)


def _invalid(code: str, detail: str) -> RuleHit:
    return _hit(RULE_INVALID, Decision.DENY, code, detail)


def _is_money(value: object, *, allow_zero: bool) -> bool:
    """True for a finite amount in whole cents, within sane bounds."""
    if not isinstance(value, Decimal) or not value.is_finite():
        return False
    if value < 0 or (value == 0 and not allow_zero) or value > MAX_AMOUNT:
        return False
    try:
        return value.quantize(CENT) == value
    except InvalidOperation:
        return False


# ------------------------------------------------------------------ validation


def _check_envelope(case: _Case) -> list[RuleHit]:
    proposal = case.proposal
    hits: list[RuleHit] = []
    if proposal.tool not in {tool.value for tool in ToolAction}:
        hits.append(_invalid("unknown_tool", "The requested tool is not allowed."))
    request_id = proposal.request_id
    if not REQUEST_ID_PATTERN.fullmatch(request_id or ""):
        hits.append(
            _invalid("bad_request_id", "The request id is missing or too long.")
        )
    if not _is_money(proposal.amount, allow_zero=False):
        hits.append(_invalid("bad_amount", "The amount is not a positive money value."))
    if not CURRENCY_PATTERN.match(proposal.currency or ""):
        hits.append(_invalid("bad_currency", "The currency is not a 3-letter code."))
    return hits


def _check_required_fields(case: _Case) -> list[RuleHit]:
    proposal = case.proposal
    if proposal.tool not in {tool.value for tool in ToolAction}:
        return []
    tool = ToolAction(proposal.tool)
    missing: list[str] = []
    needs_order = tool in (ToolAction.CREATE_REFUND, ToolAction.ACCEPT_DISPUTE_CLAIM)
    if needs_order and not proposal.order_id:
        missing.append("order_id")
    if tool is ToolAction.ACCEPT_DISPUTE_CLAIM and not proposal.dispute_id:
        missing.append("dispute_id")
    if tool is ToolAction.PAYOUT:
        if not proposal.payee_id:
            missing.append("payee_id")
        if not proposal.reference:
            missing.append("reference")
    if not missing:
        return []
    return [_invalid("missing_field", f"Missing: {', '.join(missing)}.")]


def _evidence_problem(evidence: OrderEvidence, now: datetime) -> str | None:
    if not (evidence.order_id and evidence.account_id):
        return "The record has no order or account id."
    if not CURRENCY_PATTERN.match(evidence.currency or ""):
        return "The record has no valid currency."
    if not _is_money(evidence.captured_amount, allow_zero=True):
        return "The captured amount is not valid."
    if not _is_money(evidence.refunded_amount, allow_zero=True):
        return "The refunded amount is not valid."
    if evidence.refunded_amount > evidence.captured_amount:
        return "More was refunded than was captured."
    if evidence.dispute_amount is not None and not _is_money(
        evidence.dispute_amount, allow_zero=True
    ):
        return "The dispute amount is not valid."
    if evidence.fetched_at.tzinfo is None or now.tzinfo is None:
        return "A timestamp has no time zone."
    return None


def _check_evidence(case: _Case) -> list[RuleHit]:
    if case.proposal.tool == ToolAction.PAYOUT.value:
        return []
    if case.proposal.tool not in {tool.value for tool in ToolAction}:
        return []
    if case.evidence is None:
        return [_invalid("evidence_missing", "PayPal's record could not be read.")]
    problem = _evidence_problem(case.evidence, case.now)
    return [_invalid("evidence_invalid", problem)] if problem else []


def _check_ledger(case: _Case) -> list[RuleHit]:
    ledger = case.ledger
    bad = (
        not _is_money(ledger.reserved_for_order, allow_zero=True)
        or not _is_money(ledger.outflow_24h, allow_zero=True)
        or ledger.refunds_24h_for_customer < 0
        or not ledger.expected_account_id
    )
    return (
        [_invalid("ledger_invalid", "Internal counters are not valid.")] if bad else []
    )


_VALIDATORS: tuple[Callable[[_Case], list[RuleHit]], ...] = (
    _check_envelope,
    _check_required_fields,
    _check_evidence,
    _check_ledger,
)


def _validate(case: _Case) -> list[RuleHit]:
    hits: list[RuleHit] = []
    for validator in _VALIDATORS:
        hits.extend(validator(case))
    return hits


# ----------------------------------------------------------------------- rules


def _rule_stopped(case: _Case) -> RuleHit | None:
    """While the merchant has stopped refunds, every money action is refused."""
    if not case.ledger.refunds_stopped:
        return None
    return _hit(
        RULE_STOPPED,
        Decision.DENY,
        "refunds_stopped",
        "the merchant has stopped all refunds",
    )


def _rule_scope(case: _Case) -> RuleHit | None:
    """Currency and account must match the policy and the order."""
    problems: list[str] = []
    if case.proposal.currency != case.policy.currency:
        problems.append("currency differs from the policy currency")
    evidence = case.evidence
    if evidence is not None:
        if case.proposal.currency != evidence.currency:
            problems.append("currency differs from the order currency")
        if evidence.account_id != case.ledger.expected_account_id:
            problems.append("the order belongs to a different account")
    if not problems:
        return None
    return _hit(
        RULE_MISMATCH_SCOPE, Decision.DENY, "scope_mismatch", "; ".join(problems)
    )


def _dispute_problems(case: _Case, evidence: OrderEvidence) -> list[str]:
    if case.tool is not ToolAction.ACCEPT_DISPUTE_CLAIM:
        return []
    if not evidence.has_open_dispute or evidence.dispute_id is None:
        return ["PayPal shows no open dispute on this order"]
    problems: list[str] = []
    if case.proposal.dispute_id != evidence.dispute_id:
        problems.append("the dispute id differs from PayPal's record")
    if evidence.dispute_amount != case.proposal.amount:
        problems.append("the amount differs from the disputed amount")
    return problems


def _claim_problems(proposal: Proposal, evidence: OrderEvidence) -> list[str]:
    """Compare what the agent claimed about the order with PayPal's record."""
    problems: list[str] = []
    if proposal.order_id is not None and proposal.order_id != evidence.order_id:
        problems.append("the order id differs from PayPal's record")
    claimed = proposal.claimed_captured
    if claimed is not None and claimed != evidence.captured_amount:
        problems.append("the captured amount differs from PayPal's record")
    claimed = proposal.claimed_refunded
    if claimed is not None and claimed != evidence.refunded_amount:
        problems.append("the refunded amount differs from PayPal's record")
    return problems


def _rule_evidence(case: _Case) -> RuleHit | None:
    """Agent claims and evidence age must agree with PayPal's record."""
    problems: list[str] = []
    evidence = case.evidence
    if evidence is not None:
        age = (case.now - evidence.fetched_at).total_seconds()
        if age < 0 or age > case.policy.evidence_max_age_seconds:
            problems.append("the evidence is too old to rely on")
        problems.extend(_claim_problems(case.proposal, evidence))
        problems.extend(_dispute_problems(case, evidence))
    if not problems:
        return None
    return _hit(RULE_EVIDENCE, Decision.DENY, "claims_differ", "; ".join(problems))


def _reading_problems(proposal: Proposal, reading: CustomerReading) -> list[str]:
    """Where the customer's own words fail to back the proposal exactly."""
    problems: list[str] = []
    if reading.order_id is None:
        problems.append("the customer named no order")
    elif reading.order_id != proposal.order_id:
        problems.append("the customer named a different order")
    if reading.amount is None:
        problems.append("the customer named no amount")
    elif reading.amount != proposal.amount:
        problems.append("the customer asked for a different amount")
    if reading.currency is not None and reading.currency != proposal.currency:
        problems.append("the customer wrote a different currency")
    if reading.wants_other_destination:
        problems.append("the customer asked for the money to go somewhere else")
    if reading.reason in UNPROVABLE_REASONS:
        problems.append("PayPal's record for this order cannot confirm the reason")
    return problems


def _rule_customer_words(case: _Case) -> RuleHit | None:
    """A refund goes out alone only when the customer's own message, read
    separately from the assistant, asks for exactly this; a reading never
    refuses, it asks the merchant."""
    signals = case.signals
    if case.tool is not ToolAction.CREATE_REFUND:
        return None
    if signals.reading_status is ReadingStatus.NOT_SUPPLIED:
        return None
    if signals.reading_status is ReadingStatus.READ and signals.reading is not None:
        problems = _reading_problems(case.proposal, signals.reading)
    else:
        problems = ["the customer's message could not be read"]
    if not problems:
        return None
    return _hit(
        RULE_CUSTOMER_WORDS, Decision.ESCALATE, "customer_words", "; ".join(problems)
    )


def _rule_injection(case: _Case) -> RuleHit | None:
    """A screening verdict or a deterministic pre-screen hit denies."""
    signals = case.signals
    if signals.screening is ScreeningVerdict.INJECTION or signals.prescreen_hit:
        return _hit(
            RULE_INJECTION,
            Decision.DENY,
            "injection_suspected",
            "The request text looks like an attempt to steer the assistant.",
        )
    return None


def _rule_screening_down(case: _Case) -> RuleHit | None:
    """Unavailable screening escalates (or denies by configuration), never allows."""
    if case.signals.screening is not ScreeningVerdict.UNAVAILABLE:
        return None
    denies = case.screening.on_unavailable == "deny"
    return _hit(
        RULE_SCREENING_DOWN,
        Decision.DENY if denies else Decision.ESCALATE,
        "screening_unavailable",
        "The safety screening could not run, so a person must decide.",
    )


def _rule_cumulative(case: _Case) -> RuleHit | None:
    """Money returned on an order must not exceed what was captured.

    Counts refunds and conceded disputes, made or reserved, plus this one.
    """
    evidence = case.evidence
    returns_money = (ToolAction.CREATE_REFUND, ToolAction.ACCEPT_DISPUTE_CLAIM)
    if case.tool not in returns_money or evidence is None:
        return None
    committed = evidence.refunded_amount + case.ledger.reserved_for_order
    if committed + case.proposal.amount <= evidence.captured_amount:
        return None
    return _hit(
        RULE_CUMULATIVE,
        Decision.DENY,
        "exceeds_captured",
        f"{committed} is already refunded or reserved; "
        f"{case.proposal.amount} more would pass the "
        f"{evidence.captured_amount} captured.",
    )


def _rule_duplicate_payout(case: _Case) -> RuleHit | None:
    """The same payee, amount and reference inside the window is a duplicate."""
    if case.tool is ToolAction.PAYOUT and case.ledger.duplicate_payout_in_window:
        return _hit(
            RULE_DUPLICATE_PAYOUT,
            Decision.DENY,
            "duplicate_payout",
            "The same payee, amount and reference was already paid recently.",
        )
    return None


def _rule_daily_cap(case: _Case) -> RuleHit | None:
    """Executed plus reserved outflow in 24 hours must stay under the cap."""
    projected = case.ledger.outflow_24h + case.proposal.amount
    if projected <= case.policy.daily_outflow_cap:
        return None
    return _hit(
        RULE_DAILY_CAP,
        Decision.DENY,
        "daily_cap",
        f"{projected} would pass the daily cap of {case.policy.daily_outflow_cap}.",
    )


def _rule_amount_limits(case: _Case) -> RuleHit | None:
    """Above the maximum denies; above the automatic limit escalates."""
    if not case.moves_by_amount:
        return None
    amount, policy = case.proposal.amount, case.policy
    if amount > policy.refund_max_limit:
        return _hit(
            RULE_ABOVE_MAX,
            Decision.DENY,
            "above_max",
            f"{amount} is above the maximum of {policy.refund_max_limit}.",
        )
    if amount > policy.refund_auto_limit:
        return _hit(
            RULE_ABOVE_AUTO,
            Decision.ESCALATE,
            "above_auto",
            f"{amount} is above the automatic limit of {policy.refund_auto_limit}.",
        )
    return None


def _rule_velocity(case: _Case) -> RuleHit | None:
    """A refund past the customer's 24-hour limit is paused until they are under it.

    Pausing instead of refusing keeps genuine refunds inside the gateway (a flat
    refusal sends them to PayPal directly, outside every check) while nobody,
    including the merchant, can push an extra refund through early.
    """
    if case.tool is not ToolAction.CREATE_REFUND:
        return None
    count = case.ledger.refunds_24h_for_customer + 1
    if count <= case.policy.velocity_max_refunds_24h:
        return None
    return _hit(
        RULE_VELOCITY,
        Decision.PAUSE,
        "velocity",
        f"This would be refund {count} in 24 hours; "
        f"the limit is {case.policy.velocity_max_refunds_24h}.",
    )


def _rule_auto_budget(case: _Case) -> RuleHit | None:
    """Refunds sent without asking stay within the automatic limit per order, and
    per customer per day, so one refund cannot be split into several small ones
    that each pass on their own."""
    if case.tool is not ToolAction.CREATE_REFUND:
        return None
    amount, limit = case.proposal.amount, case.policy.refund_auto_limit
    if amount > limit:
        return None  # the amount limits already ask a person or refuse
    order_total = case.ledger.auto_for_order + amount
    customer_total = case.ledger.auto_24h_for_customer + amount
    if order_total <= limit and customer_total <= limit:
        return None
    return _hit(
        RULE_AUTO_BUDGET,
        Decision.ESCALATE,
        "auto_budget",
        f"{max(order_total, customer_total)} sent without asking would pass the "
        f"automatic limit of {limit} for this order or customer today.",
    )


def _rule_open_dispute(case: _Case) -> RuleHit | None:
    """A refund on an order with an open dispute risks paying twice."""
    evidence = case.evidence
    disputed = evidence is not None and evidence.has_open_dispute
    if case.tool is ToolAction.CREATE_REFUND and disputed:
        return _hit(
            RULE_OPEN_DISPUTE,
            Decision.ESCALATE,
            "open_dispute",
            "A refund now could be paid twice, once here and once as a chargeback.",
        )
    return None


def _rule_dispute_concession(case: _Case) -> RuleHit | None:
    """Accepting a dispute claim concedes funds, so a person always decides."""
    if case.tool is ToolAction.ACCEPT_DISPUTE_CLAIM:
        return _hit(
            RULE_DISPUTE_CONCESSION,
            Decision.ESCALATE,
            "concedes_funds",
            "Accepting the claim gives up the disputed amount.",
        )
    return None


def _rule_new_payee(case: _Case) -> RuleHit | None:
    """A new or changed payee is escalated or denied by configuration, never allowed."""
    if case.tool is not ToolAction.PAYOUT or case.ledger.known_payee:
        return None
    denies = case.policy.payout_new_payee_action == "deny"
    return _hit(
        RULE_NEW_PAYEE,
        Decision.DENY if denies else Decision.ESCALATE,
        "new_payee",
        "This payee has not been paid before or their details changed.",
    )


RULES: tuple[Rule, ...] = (
    _rule_stopped,
    _rule_scope,
    _rule_evidence,
    _rule_injection,
    _rule_screening_down,
    _rule_cumulative,
    _rule_duplicate_payout,
    _rule_daily_cap,
    _rule_amount_limits,
    _rule_velocity,
    _rule_auto_budget,
    _rule_open_dispute,
    _rule_dispute_concession,
    _rule_new_payee,
    _rule_customer_words,
)


# ------------------------------------------------------------------- combining


def release_time(ledger: LedgerContext, limit: int, now: datetime) -> datetime:
    """When the customer's count in the last 24 hours drops below the limit.

    With the refunds sorted oldest first, the request fits again once all but
    ``limit - 1`` of them have left the window. Missing times pause a full day.
    """
    times = sorted(ledger.customer_refund_times)
    index = len(times) - limit
    if index < 0 or len(times) < ledger.refunds_24h_for_customer:
        return now + VELOCITY_WINDOW
    return max(times[index] + VELOCITY_WINDOW, now + MIN_PAUSE)


def _decision_of(hits: list[RuleHit]) -> Decision:
    """Strictest wins: refuse, then pause, then ask a person, then allow."""
    for decision in (Decision.DENY, Decision.PAUSE, Decision.ESCALATE):
        if any(h.decision is decision for h in hits):
            return decision
    return Decision.ALLOW


def _deciding(hits: list[RuleHit], decision: Decision) -> RuleHit:
    order = DENY_PRIORITY if decision is Decision.DENY else ESCALATE_PRIORITY
    candidates = [h for h in hits if h.decision is decision]
    candidates.sort(key=lambda h: order.index(h.rule_id) if h.rule_id in order else 99)
    return candidates[0]


def _numeric(rule_id: str) -> int:
    return int(rule_id.removeprefix("R"))


def _result(case: _Case, hits: list[RuleHit]) -> PolicyResult:
    proposal = case.proposal
    metadata = {
        "tool": proposal.tool,
        "amount": str(proposal.amount),
        "currency": proposal.currency,
        "hits": [
            {"rule": h.rule_id, "decision": h.decision.value, "code": h.code}
            for h in hits
        ],
    }
    if not hits:
        return PolicyResult(
            decision=Decision.ALLOW,
            rule_id=RULE_ALLOW,
            explanation=_RULE_TITLES[RULE_ALLOW] + ".",
            requires_human_approval=False,
            triggered_rules=(RULE_ALLOW,),
            metadata=metadata,
        )
    decision = _decision_of(hits)
    winner = _deciding(hits, decision)
    triggered = sorted({h.rule_id for h in hits}, key=_numeric)
    release = None
    if decision is Decision.PAUSE:
        limit = case.policy.velocity_max_refunds_24h
        release = release_time(case.ledger, limit, case.now)
    return PolicyResult(
        decision=decision,
        rule_id=winner.rule_id,
        explanation=f"{_RULE_TITLES[winner.rule_id]}: {winner.detail}",
        requires_human_approval=decision is Decision.ESCALATE,
        triggered_rules=tuple(triggered),
        metadata=metadata,
        release_at=release,
    )


def evaluate(
    proposal: Proposal,
    evidence: OrderEvidence | None,
    ledger: LedgerContext,
    signals: Signals,
    policy: PolicySettings,
    screening: ScreeningSettings,
    now: datetime,
) -> PolicyResult:
    """Decide a proposed action. Pure, total for malformed data, never raises on it."""
    case = _Case(proposal, evidence, ledger, signals, policy, screening, now)
    invalid = _validate(case)
    if invalid:
        return _result(case, invalid)
    hits = [hit for rule in RULES if (hit := rule(case)) is not None]
    return _result(case, hits)


def check_execution_drift(
    decided: OrderEvidence, current: OrderEvidence
) -> PolicyResult | None:
    """Compare evidence at execution time with what the decision was based on.

    Money, currency, account or order changes deny. A change of dispute state or
    more refunds having landed is plain state drift: the approver must look again.
    Returns None when nothing material changed.
    """
    fixed = (
        decided.order_id == current.order_id
        and decided.currency == current.currency
        and decided.account_id == current.account_id
        and decided.captured_amount == current.captured_amount
    )
    if not fixed:
        return PolicyResult(
            decision=Decision.DENY,
            rule_id=RULE_EVIDENCE,
            explanation=(
                f"{_RULE_TITLES[RULE_EVIDENCE]}: the order changed after approval."
            ),
            triggered_rules=(RULE_EVIDENCE,),
            metadata={"hits": [{"rule": RULE_EVIDENCE, "code": "evidence_changed"}]},
        )
    drifted = (
        decided.has_open_dispute != current.has_open_dispute
        or decided.refunded_amount != current.refunded_amount
    )
    if not drifted:
        return None
    # a dispute that opened meanwhile is named, so the owner is told about it
    dispute_opened = current.has_open_dispute and not decided.has_open_dispute
    return PolicyResult(
        decision=Decision.ESCALATE,
        rule_id=RULE_EVIDENCE,
        explanation=(
            f"{_RULE_TITLES[RULE_EVIDENCE]}: the order state moved after approval."
        ),
        requires_human_approval=True,
        triggered_rules=(RULE_EVIDENCE, RULE_OPEN_DISPUTE)
        if dispute_opened
        else (RULE_EVIDENCE,),
        metadata={"hits": [{"rule": RULE_EVIDENCE, "code": "state_drift"}]},
    )
