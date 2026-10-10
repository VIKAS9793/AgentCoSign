"""The policy scenario table: one row is one behaviour the engine must show.

Written before the engine and kept as the specification. Limits used here are
explicit (auto 25.00, max 250.00, 3 refunds a day, cap 1000.00) so the table does
not move when configuration is tuned; a separate test ties the table to the
shipped configuration.
"""

from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any

from agentcosign.core.config import PolicySettings, ScreeningSettings
from agentcosign.models.domain import Decision
from agentcosign.models.policy import (
    LedgerContext,
    OrderEvidence,
    Proposal,
    ScreeningVerdict,
    Signals,
)

NOW = datetime(2026, 11, 2, 12, 0, tzinfo=UTC)
ACCOUNT = "ACCT-1"
NO_EVIDENCE = object()


def D(value: str) -> Decimal:  # noqa: N802 -- short constructor keeps the table readable
    """Build an exact Decimal from a string."""
    return Decimal(value)


def policy(**overrides: Any) -> PolicySettings:
    """The reference limits the table is written against."""
    values: dict[str, Any] = {
        "currency": "USD",
        "refund_auto_limit": "25.00",
        "refund_max_limit": "250.00",
        "velocity_max_refunds_24h": 3,
        "payout_new_payee_action": "escalate",
        "payout_duplicate_window_days": 30,
        "daily_outflow_cap": "1000.00",
        "evidence_max_age_seconds": 60,
    }
    values.update(overrides)
    return PolicySettings(**values)


def screening(on_unavailable: str = "escalate") -> ScreeningSettings:
    """Screening configuration for the table."""
    return ScreeningSettings(on_unavailable=on_unavailable, max_document_chars=8000)


@dataclass(frozen=True)
class Scenario:
    """One row of the table: inputs as overrides on a clean baseline, plus outcome."""

    id: str
    title: str
    decision: Decision
    rule: str
    triggered: tuple[str, ...] | None = None
    proposal: dict[str, Any] = field(default_factory=dict)
    evidence: Any = field(default_factory=dict)
    ledger: dict[str, Any] = field(default_factory=dict)
    signals: dict[str, Any] = field(default_factory=dict)
    policy: dict[str, Any] = field(default_factory=dict)
    on_unavailable: str = "escalate"

    def build(
        self,
    ) -> tuple[
        Proposal,
        OrderEvidence | None,
        LedgerContext,
        Signals,
        PolicySettings,
        ScreeningSettings,
    ]:
        """Materialise the inputs by applying the overrides to the baseline."""
        proposal = Proposal(
            **{
                "request_id": "req-1",
                "tool": "create_refund",
                "amount": D("10.00"),
                "currency": "USD",
                "order_id": "ORD-1",
                **self.proposal,
            }
        )
        evidence: OrderEvidence | None
        if self.evidence is NO_EVIDENCE:
            evidence = None
        else:
            evidence = OrderEvidence(
                **{
                    "order_id": "ORD-1",
                    "currency": "USD",
                    "captured_amount": D("100.00"),
                    "refunded_amount": D("0.00"),
                    "account_id": ACCOUNT,
                    "has_open_dispute": False,
                    "fetched_at": NOW - timedelta(seconds=5),
                    **self.evidence,
                }
            )
        ledger = LedgerContext(**{"expected_account_id": ACCOUNT, **self.ledger})
        signals = Signals(**self.signals)
        return (
            proposal,
            evidence,
            ledger,
            signals,
            policy(**self.policy),
            screening(self.on_unavailable),
        )


A, E, X = Decision.ALLOW, Decision.ESCALATE, Decision.DENY
BIG = {"captured_amount": D("5000.00")}  # lets amount limits be tested alone
DISPUTE = {
    "has_open_dispute": True,
    "dispute_id": "DSP-1",
    "dispute_amount": D("40.00"),
    "captured_amount": D("500.00"),
}
CLAIM = {"tool": "accept_dispute_claim", "dispute_id": "DSP-1", "amount": D("40.00")}
PAYOUT = {
    "tool": "payout",
    "order_id": None,
    "payee_id": "PAYEE-1",
    "reference": "INV-9",
    "amount": D("20.00"),
}

SCENARIOS: tuple[Scenario, ...] = (
    # --- amount boundaries (refunds) -------------------------------------------
    Scenario("S01", "Small clean refund is allowed", A, "R1", ("R1",)),
    Scenario(
        "S02",
        "Exactly the automatic limit is allowed",
        A,
        "R1",
        proposal={"amount": D("25.00")},
        evidence=BIG,
    ),
    Scenario(
        "S03",
        "One cent above the automatic limit escalates",
        E,
        "R2",
        ("R2",),
        proposal={"amount": D("25.01")},
        evidence=BIG,
    ),
    Scenario(
        "S04",
        "Mid-range refund escalates",
        E,
        "R2",
        ("R2",),
        proposal={"amount": D("100.00")},
        evidence=BIG,
    ),
    Scenario(
        "S05",
        "Exactly the maximum limit escalates",
        E,
        "R2",
        ("R2",),
        proposal={"amount": D("250.00")},
        evidence=BIG,
    ),
    Scenario(
        "S06",
        "One cent above the maximum is denied",
        X,
        "R3",
        ("R3",),
        proposal={"amount": D("250.01")},
        evidence=BIG,
    ),
    Scenario(
        "S07", "The smallest refund is allowed", A, "R1", proposal={"amount": D("0.01")}
    ),
    Scenario(
        "S08",
        "A very large refund is denied",
        X,
        "R3",
        ("R3",),
        proposal={"amount": D("1000.00")},
        evidence=BIG,
    ),
    # --- cumulative refunds and reservations -----------------------------------
    Scenario(
        "S09",
        "Refund that exactly uses the remainder is allowed",
        A,
        "R1",
        proposal={"amount": D("20.00")},
        evidence={"refunded_amount": D("80.00")},
    ),
    Scenario(
        "S10",
        "One cent past the remainder is denied",
        X,
        "R4",
        ("R4",),
        proposal={"amount": D("20.01")},
        evidence={"refunded_amount": D("80.00")},
    ),
    Scenario(
        "S11",
        "Reservations count toward the remainder (boundary allowed)",
        A,
        "R1",
        proposal={"amount": D("15.00")},
        evidence={"refunded_amount": D("70.00")},
        ledger={"reserved_for_order": D("15.00")},
    ),
    Scenario(
        "S12",
        "Reservations count toward the remainder (one cent over)",
        X,
        "R4",
        ("R4",),
        proposal={"amount": D("15.01")},
        evidence={"refunded_amount": D("70.00")},
        ledger={"reserved_for_order": D("15.00")},
    ),
    Scenario(
        "S13",
        "A full refund escalates",
        E,
        "R2",
        ("R2",),
        proposal={"amount": D("100.00")},
    ),
    Scenario(
        "S14",
        "Refunding more than captured is denied above the escalation",
        X,
        "R4",
        ("R2", "R4"),
        proposal={"amount": D("100.01")},
    ),
    Scenario(
        "S15",
        "Nothing captured means no refund",
        X,
        "R4",
        ("R4",),
        proposal={"amount": D("5.00")},
        evidence={"captured_amount": D("0.00")},
    ),
    # --- velocity ----------------------------------------------------------------
    Scenario(
        "S16",
        "Third refund in a day is allowed",
        A,
        "R1",
        ledger={"refunds_24h_for_customer": 2},
    ),
    Scenario(
        "S17",
        "Fourth refund in a day escalates",
        E,
        "R5",
        ("R5",),
        ledger={"refunds_24h_for_customer": 3},
    ),
    Scenario(
        "S18",
        "Velocity and amount both escalate; velocity decides",
        E,
        "R5",
        ("R2", "R5"),
        proposal={"amount": D("50.00")},
        evidence=BIG,
        ledger={"refunds_24h_for_customer": 3},
    ),
    # --- disputes ----------------------------------------------------------------
    Scenario(
        "S19",
        "Refund on a disputed order escalates",
        E,
        "R6",
        ("R6",),
        evidence={"has_open_dispute": True, "dispute_id": "DSP-1"},
    ),
    Scenario(
        "S20",
        "Open dispute and above maximum: denied",
        X,
        "R3",
        ("R3", "R6"),
        proposal={"amount": D("300.00")},
        evidence={**BIG, "has_open_dispute": True, "dispute_id": "DSP-1"},
    ),
    Scenario(
        "S21",
        "Accepting a dispute claim always escalates",
        E,
        "R7",
        ("R7",),
        proposal=CLAIM,
        evidence=DISPUTE,
    ),
    Scenario(
        "S22",
        "Even a tiny dispute concession escalates",
        E,
        "R7",
        ("R7",),
        proposal={**CLAIM, "amount": D("1.00")},
        evidence={**DISPUTE, "dispute_amount": D("1.00")},
    ),
    Scenario(
        "S23",
        "Accepting a dispute that PayPal does not show is denied",
        X,
        "R14",
        ("R7", "R14"),
        proposal=CLAIM,
        evidence={"captured_amount": D("500.00")},
    ),
    Scenario(
        "S24",
        "Accepting with the wrong dispute id is denied",
        X,
        "R14",
        ("R7", "R14"),
        proposal={**CLAIM, "dispute_id": "DSP-OTHER"},
        evidence=DISPUTE,
    ),
    Scenario(
        "S25",
        "Accepting for a different amount than disputed is denied",
        X,
        "R14",
        ("R7", "R14"),
        proposal={**CLAIM, "amount": D("39.99")},
        evidence=DISPUTE,
    ),
    Scenario(
        "S26",
        "Dispute above the maximum limit still only escalates",
        E,
        "R7",
        ("R7",),
        proposal={**CLAIM, "amount": D("900.00")},
        evidence={
            **DISPUTE,
            "dispute_amount": D("900.00"),
            "captured_amount": D("5000.00"),
        },
    ),
    Scenario(
        "S27",
        "Dispute concession past the daily cap is denied",
        X,
        "R10",
        ("R7", "R10"),
        proposal={**CLAIM, "amount": D("300.00")},
        evidence={**DISPUTE, "dispute_amount": D("300.00")},
        ledger={"outflow_24h": D("800.00")},
    ),
    # --- daily cap ----------------------------------------------------------------
    Scenario(
        "S28",
        "Reaching the daily cap exactly is allowed",
        A,
        "R1",
        proposal={"amount": D("25.00")},
        evidence=BIG,
        ledger={"outflow_24h": D("975.00")},
    ),
    Scenario(
        "S29",
        "Over the cap and over the automatic limit: cap denies",
        X,
        "R10",
        ("R2", "R10"),
        proposal={"amount": D("25.01")},
        evidence=BIG,
        ledger={"outflow_24h": D("975.00")},
    ),
    Scenario(
        "S30",
        "A small refund that crosses the cap is denied",
        X,
        "R10",
        ("R10",),
        proposal={"amount": D("10.01")},
        ledger={"outflow_24h": D("990.00")},
    ),
    Scenario(
        "S31",
        "One cent beyond a full cap is denied",
        X,
        "R10",
        ("R10",),
        proposal={"amount": D("0.01")},
        ledger={"outflow_24h": D("1000.00")},
    ),
    # --- injection and screening ------------------------------------------------
    Scenario(
        "S32",
        "Screening verdict injection is denied",
        X,
        "R11",
        ("R11",),
        signals={"screening": ScreeningVerdict.INJECTION},
    ),
    Scenario(
        "S33",
        "Deterministic pre-screen hit is denied",
        X,
        "R11",
        ("R11",),
        signals={"prescreen_hit": True},
    ),
    Scenario(
        "S34",
        "Screening unavailable escalates",
        E,
        "R15",
        ("R15",),
        signals={"screening": ScreeningVerdict.UNAVAILABLE},
    ),
    Scenario(
        "S35",
        "Screening unavailable denies when configured to",
        X,
        "R15",
        ("R15",),
        signals={"screening": ScreeningVerdict.UNAVAILABLE},
        on_unavailable="deny",
    ),
    Scenario(
        "S36",
        "Injection outranks an open dispute",
        X,
        "R11",
        ("R6", "R11"),
        signals={"screening": ScreeningVerdict.INJECTION},
        evidence={"has_open_dispute": True, "dispute_id": "DSP-1"},
    ),
    Scenario(
        "S37",
        "Screening down plus high amount still escalates",
        E,
        "R15",
        ("R2", "R15"),
        proposal={"amount": D("50.00")},
        evidence=BIG,
        signals={"screening": ScreeningVerdict.UNAVAILABLE},
    ),
    # --- currency and account ----------------------------------------------------
    Scenario(
        "S38",
        "Proposal currency differs from the order",
        X,
        "R13",
        ("R13",),
        proposal={"currency": "EUR"},
    ),
    Scenario(
        "S39",
        "Order currency differs from the proposal",
        X,
        "R13",
        ("R13",),
        evidence={"currency": "EUR"},
    ),
    Scenario(
        "S40",
        "Order belongs to another account",
        X,
        "R13",
        ("R13",),
        evidence={"account_id": "ACCT-OTHER"},
    ),
    Scenario(
        "S41",
        "Order and proposal agree but not with the policy currency",
        X,
        "R13",
        ("R13",),
        proposal={"currency": "EUR"},
        evidence={"currency": "EUR"},
    ),
    # --- claims versus evidence --------------------------------------------------
    Scenario(
        "S42",
        "Claimed captured amount is wrong",
        X,
        "R14",
        ("R14",),
        proposal={"claimed_captured": D("999.00")},
    ),
    Scenario(
        "S43",
        "Claimed refunded amount is wrong",
        X,
        "R14",
        ("R14",),
        proposal={"claimed_refunded": D("50.00")},
    ),
    Scenario(
        "S44",
        "Correct claims do not change the outcome",
        A,
        "R1",
        proposal={"claimed_captured": D("100.00"), "claimed_refunded": D("0.00")},
    ),
    Scenario(
        "S45",
        "Amount read from the source text differs",
        X,
        "R14",
        ("R14",),
        signals={"extracted_amount": D("100.00")},
    ),
    Scenario(
        "S46",
        "Amount read from the source text agrees",
        A,
        "R1",
        signals={"extracted_amount": D("10.00")},
    ),
    Scenario(
        "S47",
        "Proposal names a different order than the record",
        X,
        "R14",
        ("R14",),
        proposal={"order_id": "ORD-OTHER"},
    ),
    Scenario(
        "S48",
        "Evidence older than the limit is not trusted",
        X,
        "R14",
        ("R14",),
        evidence={"fetched_at": NOW - timedelta(seconds=61)},
    ),
    Scenario(
        "S49",
        "Evidence exactly at the age limit is trusted",
        A,
        "R1",
        evidence={"fetched_at": NOW - timedelta(seconds=60)},
    ),
    Scenario(
        "S50",
        "Evidence stamped in the future is not trusted",
        X,
        "R14",
        ("R14",),
        evidence={"fetched_at": NOW + timedelta(seconds=30)},
    ),
    # --- malformed input fails closed -------------------------------------------
    Scenario(
        "S51",
        "Unknown tool is denied",
        X,
        "R12",
        ("R12",),
        proposal={"tool": "transfer_funds"},
    ),
    Scenario(
        "S52",
        "Zero amount is denied",
        X,
        "R12",
        ("R12",),
        proposal={"amount": D("0.00")},
    ),
    Scenario(
        "S53",
        "Negative amount is denied",
        X,
        "R12",
        ("R12",),
        proposal={"amount": D("-5.00")},
    ),
    Scenario(
        "S54",
        "Not-a-number amount is denied",
        X,
        "R12",
        ("R12",),
        proposal={"amount": D("NaN")},
    ),
    Scenario(
        "S55",
        "Sub-cent precision is denied",
        X,
        "R12",
        ("R12",),
        proposal={"amount": D("10.001")},
    ),
    Scenario(
        "S56",
        "Infinite amount is denied",
        X,
        "R12",
        ("R12",),
        proposal={"amount": D("Infinity")},
    ),
    Scenario(
        "S57",
        "Missing order id is denied",
        X,
        "R12",
        ("R12",),
        proposal={"order_id": None},
    ),
    Scenario(
        "S58",
        "Empty request id is denied",
        X,
        "R12",
        ("R12",),
        proposal={"request_id": ""},
    ),
    Scenario(
        "S59",
        "No evidence from PayPal is denied",
        X,
        "R12",
        ("R12",),
        evidence=NO_EVIDENCE,
    ),
    Scenario(
        "S60",
        "Evidence that refunds more than captured is denied",
        X,
        "R12",
        ("R12",),
        evidence={"refunded_amount": D("150.00")},
    ),
    Scenario(
        "S61",
        "Lower-case currency code is denied",
        X,
        "R12",
        ("R12",),
        proposal={"currency": "usd"},
    ),
    Scenario(
        "S62",
        "Evidence timestamp without a time zone is denied",
        X,
        "R12",
        ("R12",),
        evidence={"fetched_at": datetime(2026, 11, 2, 11, 59, 55)},
    ),
    Scenario(
        "S63",
        "A negative internal counter is denied",
        X,
        "R12",
        ("R12",),
        ledger={"refunds_24h_for_customer": -1},
    ),
    Scenario(
        "S64",
        "An absurdly large amount is denied as invalid",
        X,
        "R12",
        ("R12",),
        proposal={"amount": D("10000000000.00")},
    ),
    # --- payouts -----------------------------------------------------------------
    Scenario(
        "S65",
        "Small payout to a known payee is allowed",
        A,
        "R1",
        proposal=PAYOUT,
        evidence=NO_EVIDENCE,
        ledger={"known_payee": True},
    ),
    Scenario(
        "S66",
        "Payout to a new payee escalates",
        E,
        "R8",
        ("R8",),
        proposal=PAYOUT,
        evidence=NO_EVIDENCE,
    ),
    Scenario(
        "S67",
        "Payout to a new payee denies when configured to",
        X,
        "R8",
        ("R8",),
        proposal=PAYOUT,
        evidence=NO_EVIDENCE,
        policy={"payout_new_payee_action": "deny"},
    ),
    Scenario(
        "S68",
        "Duplicate payout is denied",
        X,
        "R9",
        ("R9",),
        proposal=PAYOUT,
        evidence=NO_EVIDENCE,
        ledger={"known_payee": True, "duplicate_payout_in_window": True},
    ),
    Scenario(
        "S69",
        "Larger payout to a known payee escalates",
        E,
        "R2",
        ("R2",),
        proposal={**PAYOUT, "amount": D("100.00")},
        evidence=NO_EVIDENCE,
        ledger={"known_payee": True},
    ),
    Scenario(
        "S70",
        "Payout above the maximum is denied",
        X,
        "R3",
        ("R3",),
        proposal={**PAYOUT, "amount": D("251.00")},
        evidence=NO_EVIDENCE,
        ledger={"known_payee": True},
    ),
    Scenario(
        "S71",
        "Payout without a payee is denied",
        X,
        "R12",
        ("R12",),
        proposal={**PAYOUT, "payee_id": None},
        evidence=NO_EVIDENCE,
    ),
    Scenario(
        "S72",
        "Payout without a reference is denied",
        X,
        "R12",
        ("R12",),
        proposal={**PAYOUT, "reference": None},
        evidence=NO_EVIDENCE,
    ),
    Scenario(
        "S73",
        "Payout in the wrong currency is denied",
        X,
        "R13",
        ("R13",),
        proposal={**PAYOUT, "currency": "EUR"},
        evidence=NO_EVIDENCE,
        ledger={"known_payee": True},
    ),
    Scenario(
        "S74",
        "Duplicate payout to a new payee: duplicate decides",
        X,
        "R9",
        ("R8", "R9"),
        proposal=PAYOUT,
        evidence=NO_EVIDENCE,
        ledger={"duplicate_payout_in_window": True},
    ),
    Scenario(
        "S75",
        "Payout past the daily cap is denied",
        X,
        "R10",
        ("R10",),
        proposal=PAYOUT,
        evidence=NO_EVIDENCE,
        ledger={"known_payee": True, "outflow_24h": D("990.00")},
    ),
    # --- priority between rules ---------------------------------------------------
    Scenario(
        "S76",
        "Invalid input short-circuits every other rule",
        X,
        "R12",
        ("R12",),
        proposal={"amount": D("-1.00")},
        signals={"screening": ScreeningVerdict.INJECTION},
    ),
    Scenario(
        "S77",
        "Scope mismatch outranks a claims mismatch",
        X,
        "R13",
        ("R13", "R14"),
        proposal={"currency": "EUR", "claimed_captured": D("1.00")},
    ),
    Scenario(
        "S78",
        "Cumulative cap outranks the maximum limit",
        X,
        "R4",
        ("R3", "R4"),
        proposal={"amount": D("300.00")},
        evidence={"captured_amount": D("200.00")},
    ),
    # --- request id must be usable as PayPal's idempotency header -----------------
    Scenario(
        "S79",
        "A 38-character request id is accepted",
        A,
        "R1",
        proposal={"request_id": "a" * 38},
    ),
    Scenario(
        "S80",
        "A 39-character request id is denied (PayPal accepts at most 38)",
        X,
        "R12",
        ("R12",),
        proposal={"request_id": "a" * 39},
    ),
    Scenario(
        "S81",
        "A request id that could break an HTTP header is denied",
        X,
        "R12",
        ("R12",),
        proposal={"request_id": "req-1\r\nX-Injected: 1"},
    ),
    Scenario(
        "S82",
        "A request id with a trailing newline is denied",
        X,
        "R12",
        ("R12",),
        proposal={"request_id": "req-1\n"},
    ),
)

# Evidence drift between approval and execution: (id, change, expected, rule).
DRIFT_SCENARIOS: tuple[tuple[str, dict[str, Any], Decision | None], ...] = (
    ("D1", {}, None),
    ("D2", {"has_open_dispute": True}, Decision.ESCALATE),
    ("D3", {"refunded_amount": D("20.00")}, Decision.ESCALATE),
    ("D4", {"captured_amount": D("90.00")}, Decision.DENY),
    ("D5", {"currency": "EUR"}, Decision.DENY),
    ("D6", {"account_id": "ACCT-OTHER"}, Decision.DENY),
    ("D7", {"order_id": "ORD-OTHER"}, Decision.DENY),
)
