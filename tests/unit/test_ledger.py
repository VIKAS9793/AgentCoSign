"""The ledger: atomic reservations, idempotency, lifecycle and tamper evidence."""

import sqlite3
import threading
from datetime import timedelta
from decimal import Decimal
from pathlib import Path

import pytest

from agentcosign.core.exceptions import AuditIntegrityError, FailClosedError
from agentcosign.models.domain import Decision
from agentcosign.models.policy import OrderEvidence, Proposal, Signals
from agentcosign.services.identity import card_fingerprint, payee_key
from agentcosign.services.ledger import Ledger, ProposalState
from tests.scenarios.policy_scenarios import ACCOUNT, NOW, policy, screening

SECRET = b"s" * 32
WINDOW = 600  # seconds allowed to decide
SCREEN = screening()
CUSTOMER = card_fingerprint(SECRET, "VISA", "4242", "Jane Buyer")


def evidence(
    order: str = "ORD-1",
    captured: str = "100.00",
    refunded: str = "0.00",
    customer: str | None = CUSTOMER,
) -> OrderEvidence:
    return OrderEvidence(
        order_id=order,
        currency="USD",
        captured_amount=Decimal(captured),
        refunded_amount=Decimal(refunded),
        account_id=ACCOUNT,
        has_open_dispute=False,
        fetched_at=NOW - timedelta(seconds=5),
        customer_key=customer,
    )


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


def decide(ledger: Ledger, proposal: Proposal, ev: OrderEvidence | None = None):  # noqa: ANN201
    return ledger.decide(
        proposal, ev or evidence(proposal.order_id or "ORD-1"), Signals(), SCREEN, NOW
    )


# --- decisions are recorded with the right state ----------
def test_allow_reserves_and_moves_to_executing(ledger: Ledger) -> None:
    recorded = decide(ledger, refund("r1", "10.00"))
    assert recorded.result.decision is Decision.ALLOW
    assert recorded.state is ProposalState.EXECUTING
    assert ledger.state_of("r1") is ProposalState.EXECUTING


def test_escalate_waits_for_approval(ledger: Ledger) -> None:
    recorded = decide(ledger, refund("r1", "60.00"))
    assert recorded.result.decision is Decision.ESCALATE
    assert recorded.state is ProposalState.PENDING_APPROVAL


def test_deny_is_recorded_as_denied(ledger: Ledger) -> None:
    recorded = decide(ledger, refund("r1", "300.00"))
    assert recorded.result.decision is Decision.DENY
    assert recorded.state is ProposalState.DENIED


# --- reservations make the counters honest ----------
def test_pending_reservation_counts_against_the_captured_amount(ledger: Ledger) -> None:
    decide(ledger, refund("r1", "60.00"))  # escalated, reserved
    second = decide(ledger, refund("r2", "50.00"))
    assert second.result.decision is Decision.DENY
    assert second.result.rule_id == "R4"


def test_denied_and_expired_requests_do_not_reserve(ledger: Ledger) -> None:
    decide(ledger, refund("r1", "300.00"))  # denied
    decide(ledger, refund("r2", "60.00"))  # pending
    ledger.expire_due(NOW + timedelta(seconds=601), open_seconds=600)
    third = ledger.decide(
        refund("r3", "60.00"),
        replace_time(evidence(), NOW + timedelta(seconds=601)),
        Signals(),
        SCREEN,
        NOW + timedelta(seconds=601),
    )
    assert third.result.decision is Decision.ESCALATE  # 60 fits again


def test_daily_cap_counts_reservations_across_orders(ledger: Ledger) -> None:
    for index in range(4):  # 4 x 240 = 960 reserved, each escalated on its own order
        order = f"ORD-{index}"
        decide(
            ledger,
            refund(f"r{index}", "240.00", order),
            evidence(order, "1000.00", customer=None),
        )
    over = decide(
        ledger,
        refund("r9", "41.00", "ORD-9"),
        evidence("ORD-9", "1000.00", customer=None),
    )
    assert over.result.decision is Decision.DENY
    assert over.result.rule_id == "R10"


def test_velocity_counts_one_customer_across_orders(ledger: Ledger) -> None:
    for index in range(3):
        order = f"ORD-{index}"
        result = decide(ledger, refund(f"r{index}", "5.00", order), evidence(order))
        assert result.result.decision is Decision.ALLOW
    fourth = decide(ledger, refund("r3", "5.00", "ORD-3"), evidence("ORD-3"))
    assert fourth.result.decision is Decision.PAUSE
    assert fourth.result.rule_id == "R5"


def test_velocity_does_not_merge_different_cards(ledger: Ledger) -> None:
    other = card_fingerprint(SECRET, "VISA", "1111", "Jane Buyer")
    for index in range(3):
        order = f"ORD-{index}"
        decide(ledger, refund(f"r{index}", "5.00", order), evidence(order))
    result = decide(
        ledger, refund("r3", "5.00", "ORD-3"), evidence("ORD-3", customer=other)
    )
    assert result.result.decision is Decision.ALLOW


@pytest.mark.edge
def test_concurrent_requests_cannot_overspend_one_order(ledger: Ledger) -> None:
    outcomes: list[Decision] = []
    lock = threading.Lock()

    def worker(index: int) -> None:
        recorded = decide(ledger, refund(f"c{index}", "30.00"))
        with lock:
            outcomes.append(recorded.result.decision)

    threads = [threading.Thread(target=worker, args=(i,)) for i in range(20)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    granted = [d for d in outcomes if d is not Decision.DENY]
    assert len(outcomes) == 20
    assert len(granted) == 3  # 3 x 30 = 90 fits in 100; a fourth would not


# --- idempotency ----------
def test_repeating_a_request_replays_the_original_outcome(ledger: Ledger) -> None:
    first = decide(ledger, refund("r1", "60.00"))
    again = decide(ledger, refund("r1", "60.00"))
    assert again.replayed is True
    assert again.result.decision is first.result.decision
    assert again.result.rule_id == first.result.rule_id
    assert ledger.verify_chain() == 1  # the replay wrote nothing


@pytest.mark.edge
def test_reusing_an_id_for_different_content_is_refused(ledger: Ledger) -> None:
    decide(ledger, refund("r1", "10.00"))
    reused = decide(ledger, refund("r1", "99.00"))
    assert reused.result.decision is Decision.DENY
    assert reused.state is None
    assert ledger.state_of("r1") is ProposalState.EXECUTING


def test_invalid_request_ids_are_decided_but_not_stored(ledger: Ledger) -> None:
    recorded = decide(ledger, refund("", "10.00"))
    assert recorded.result.decision is Decision.DENY
    assert recorded.state is None
    assert ledger.verify_chain() == 0


# --- lifecycle ----------
def test_approval_path_and_audit_trail(ledger: Ledger) -> None:
    decide(ledger, refund("r1", "60.00"))
    ledger.transition(
        "r1",
        ProposalState.EXECUTING,
        NOW,
        {"assurance": "passkey"},
        decide_within=WINDOW,
    )
    ledger.transition("r1", ProposalState.EXECUTED, NOW, {"paypal_refund_id": "RF-1"})
    assert ledger.state_of("r1") is ProposalState.EXECUTED
    assert ledger.verify_chain() == 3


@pytest.mark.parametrize(
    ("start_amount", "target"),
    [("10.00", ProposalState.EXPIRED), ("300.00", ProposalState.EXECUTING)],
)
def test_transitions_outside_the_lifecycle_are_refused(
    ledger: Ledger, start_amount: str, target: ProposalState
) -> None:
    decide(ledger, refund("r1", start_amount))  # executing, or denied
    with pytest.raises(FailClosedError):
        ledger.transition("r1", target, NOW)


# Deadline edge cases: approve one second before, exactly at and after the
# deadline; decline after it; a caller that gives no deadline.
@pytest.mark.edge
@pytest.mark.parametrize(
    ("after", "allowed"), [(599, True), (600, False), (601, False)]
)
def test_an_approval_is_accepted_only_before_the_deadline(
    ledger: Ledger, after: int, allowed: bool
) -> None:
    decide(ledger, refund("r1", "60.00"))  # waiting for approval
    at = NOW + timedelta(seconds=after)
    if allowed:
        ledger.transition("r1", ProposalState.EXECUTING, at, decide_within=WINDOW)
        assert ledger.state_of("r1") is ProposalState.EXECUTING
        return
    with pytest.raises(FailClosedError):
        ledger.transition("r1", ProposalState.EXECUTING, at, decide_within=WINDOW)
    assert ledger.state_of("r1") is ProposalState.PENDING_APPROVAL


@pytest.mark.edge
def test_an_approval_without_a_deadline_is_refused(ledger: Ledger) -> None:
    decide(ledger, refund("r1", "60.00"))
    with pytest.raises(FailClosedError):
        ledger.transition("r1", ProposalState.EXECUTING, NOW)
    assert ledger.state_of("r1") is ProposalState.PENDING_APPROVAL


@pytest.mark.edge
def test_a_decline_after_the_deadline_is_still_accepted(ledger: Ledger) -> None:
    decide(ledger, refund("r1", "60.00"))
    ledger.transition("r1", ProposalState.DENIED, NOW + timedelta(seconds=WINDOW + 1))
    assert ledger.state_of("r1") is ProposalState.DENIED


def test_unknown_request_cannot_transition(ledger: Ledger) -> None:
    with pytest.raises(FailClosedError):
        ledger.transition("nope", ProposalState.EXECUTED, NOW)


def test_expiry_only_touches_requests_past_the_window(ledger: Ledger) -> None:
    decide(ledger, refund("r1", "60.00"))
    assert ledger.expire_due(NOW + timedelta(seconds=599), open_seconds=600) == []
    assert ledger.expire_due(NOW + timedelta(seconds=601), open_seconds=600) == ["r1"]
    assert ledger.state_of("r1") is ProposalState.EXPIRED


def test_naive_timestamps_are_refused(ledger: Ledger) -> None:
    with pytest.raises(FailClosedError):
        ledger.decide(
            refund("r1", "1.00"),
            evidence(),
            Signals(),
            SCREEN,
            NOW.replace(tzinfo=None),
        )


def test_short_audit_key_is_refused(tmp_path: Path) -> None:
    with pytest.raises(FailClosedError):
        Ledger(tmp_path / "x.db", b"short", policy(), ACCOUNT)


# --- tamper evidence ----------
def _seed(ledger: Ledger) -> None:
    for index, amount in enumerate(("10.00", "60.00", "300.00")):
        decide(
            ledger,
            refund(f"r{index}", amount, f"ORD-{index}"),
            evidence(f"ORD-{index}", "500.00"),
        )


def _db(tmp_path: Path) -> sqlite3.Connection:
    return sqlite3.connect(tmp_path / "ledger.db", isolation_level=None)


def test_untouched_chain_verifies(ledger: Ledger) -> None:
    _seed(ledger)
    assert ledger.verify_chain() == 3
    assert len(ledger.head()["entry_hash"]) == 64


def test_editing_one_row_is_detected(ledger: Ledger, tmp_path: Path) -> None:
    _seed(ledger)
    with _db(tmp_path) as conn:
        conn.execute(
            "UPDATE audit SET body = replace(body, '60.00', '6.00') WHERE seq = 2"
        )
    with pytest.raises(AuditIntegrityError):
        ledger.verify_chain()


def test_deleting_a_row_is_detected(ledger: Ledger, tmp_path: Path) -> None:
    _seed(ledger)
    with _db(tmp_path) as conn:
        conn.execute("DELETE FROM audit WHERE seq = 2")
    with pytest.raises(AuditIntegrityError):
        ledger.verify_chain()


# Checking one request: its own entries always, the chain only since the last
# point already checked (re-checking that point itself), never the whole chain.
def _count_checks(ledger: Ledger, monkeypatch: pytest.MonkeyPatch) -> list[int]:
    checked: list[int] = []
    real = ledger._verify_entry

    def counting(row: sqlite3.Row, prev: str) -> None:
        checked.append(int(row["seq"]))
        real(row, prev)

    monkeypatch.setattr(ledger, "_verify_entry", counting)
    return checked


@pytest.mark.edge
def test_checking_one_request_reads_its_entries_and_only_new_ones(
    ledger: Ledger, monkeypatch: pytest.MonkeyPatch
) -> None:
    _seed(ledger)  # three requests, one entry each, in order
    checked = _count_checks(ledger, monkeypatch)
    ledger.verify_request("r1")
    assert sorted(set(checked)) == [1, 2, 3]  # first time: up to the newest entry
    checked.clear()
    ledger.verify_request("r1")
    assert sorted(set(checked)) == [2, 3]  # its own entry, and the checked point
    decide(ledger, refund("r3", "10.00", "ORD-3"), evidence("ORD-3", "500.00"))
    checked.clear()
    ledger.verify_request("r1")
    assert sorted(set(checked)) == [2, 3, 4]  # plus the one new entry


@pytest.mark.edge
@pytest.mark.parametrize(
    "tamper",
    [
        "UPDATE audit SET body = replace(body, '60.00', '6.00') WHERE seq = 2",
        "DELETE FROM audit WHERE request_id = 'r1'",
        "UPDATE proposals SET amount = '6.00' WHERE request_id = 'r1'",
        "UPDATE audit SET body = replace(body, '300.00', '3.00') WHERE seq = 3",
    ],
    ids=["own-entry", "own-entries-deleted", "own-row", "checked-point"],
)
def test_an_edit_this_request_relies_on_is_caught_after_a_check(
    ledger: Ledger, tmp_path: Path, tamper: str
) -> None:
    _seed(ledger)
    ledger.verify_request("r1")  # everything up to entry 3 is now checked
    with _db(tmp_path) as conn:
        conn.execute(tamper)
    with pytest.raises(AuditIntegrityError):
        ledger.verify_request("r1")


@pytest.mark.edge
def test_a_new_entry_edited_after_the_check_is_caught(
    ledger: Ledger, tmp_path: Path
) -> None:
    _seed(ledger)
    ledger.verify_request("r1")
    decide(ledger, refund("r3", "10.00", "ORD-3"), evidence("ORD-3", "500.00"))
    with _db(tmp_path) as conn:
        conn.execute(
            "UPDATE audit SET body = replace(body, '10.00', '1.00') WHERE seq = 4"
        )
    with pytest.raises(AuditIntegrityError):
        ledger.verify_request("r1")


@pytest.mark.edge
def test_an_older_edit_to_another_request_is_left_to_the_full_check(
    ledger: Ledger, tmp_path: Path
) -> None:
    _seed(ledger)
    ledger.verify_request("r2")
    with _db(tmp_path) as conn:
        conn.execute(
            "UPDATE audit SET body = replace(body, '10.00', '1.00') WHERE seq = 1"
        )
    ledger.verify_request("r2")  # its own record still checks out
    with pytest.raises(AuditIntegrityError):
        ledger.verify_records()  # the scheduled full check finds it


def test_rewriting_the_whole_chain_without_the_key_is_detected(
    ledger: Ledger, tmp_path: Path
) -> None:
    from agentcosign.services.ledger import GENESIS_HASH, _entry_hash

    _seed(ledger)
    with _db(tmp_path) as conn:
        rows = conn.execute(
            "SELECT seq, created_at, request_id, event, body FROM audit ORDER BY seq"
        ).fetchall()
        prev = GENESIS_HASH
        for seq, created_at, request_id, event, body in rows:
            forged = body.replace("deny", "allow")
            entry = _entry_hash(prev, created_at, request_id, event, forged)
            conn.execute(
                "UPDATE audit SET body = ?, prev_hash = ?, entry_hash = ? "
                "WHERE seq = ?",
                (forged, prev, entry, seq),
            )
            prev = entry
    with pytest.raises(AuditIntegrityError):
        ledger.verify_chain()


def test_a_different_key_cannot_verify_the_chain(
    ledger: Ledger, tmp_path: Path
) -> None:
    _seed(ledger)
    impostor = Ledger(tmp_path / "ledger.db", b"x" * 32, policy(), ACCOUNT)
    with pytest.raises(AuditIntegrityError):
        impostor.verify_chain()


# --- privacy ----------
def test_no_card_or_payee_details_are_stored_in_clear(tmp_path: Path) -> None:
    ledger = Ledger(tmp_path / "ledger.db", SECRET, policy(), ACCOUNT)
    decide(ledger, refund("r1", "10.00"))
    payout = Proposal(
        request_id="p1",
        tool="payout",
        amount=Decimal("20.00"),
        currency="USD",
        payee_id="contractor@example.com",
        reference="INV-1",
    )
    ledger.decide(payout, None, Signals(), SCREEN, NOW)
    raw = b"".join(p.read_bytes() for p in tmp_path.glob("ledger.db*"))
    assert b"Jane Buyer" not in raw
    assert b"4242" not in raw
    assert b"contractor@example.com" not in raw


def test_fingerprint_is_stable_and_specific() -> None:
    assert card_fingerprint(SECRET, "VISA", "4242", "  jane   BUYER ") == CUSTOMER
    assert card_fingerprint(SECRET, "VISA", "4243", "Jane Buyer") != CUSTOMER
    assert card_fingerprint(SECRET, "VISA", "42", "Jane Buyer") is None
    assert card_fingerprint(SECRET, "", "4242", "Jane Buyer") is None
    assert payee_key(SECRET, "A@B.com") == payee_key(SECRET, "a@b.com")


def test_known_payee_and_duplicate_payout_come_from_history(ledger: Ledger) -> None:
    payout = Proposal(
        request_id="p1",
        tool="payout",
        amount=Decimal("20.00"),
        currency="USD",
        payee_id="PAYEE-1",
        reference="INV-1",
    )
    first = ledger.decide(payout, None, Signals(), SCREEN, NOW)
    assert first.result.rule_id == "R8"  # new payee escalates
    ledger.transition("p1", ProposalState.EXECUTING, NOW, decide_within=WINDOW)
    ledger.transition("p1", ProposalState.EXECUTED, NOW)
    repeat = Proposal(**{**_fields(payout), "request_id": "p2"})
    duplicate = ledger.decide(repeat, None, Signals(), SCREEN, NOW)
    assert duplicate.result.rule_id == "R9"
    fresh = Proposal(**{**_fields(payout), "request_id": "p3", "reference": "INV-2"})
    allowed = ledger.decide(fresh, None, Signals(), SCREEN, NOW)
    assert allowed.result.decision is Decision.ALLOW  # known payee, new reference


def _fields(proposal: Proposal) -> dict[str, object]:
    return {name: getattr(proposal, name) for name in proposal.__slots__}


def replace_time(ev: OrderEvidence, moment) -> OrderEvidence:  # noqa: ANN001
    return OrderEvidence(**{**_fields(ev), "fetched_at": moment - timedelta(seconds=5)})


@pytest.mark.parametrize("bad", ["x" * 39, "req\nid", "req id", "../r"])
@pytest.mark.edge
def test_request_ids_paypal_would_reject_are_never_stored(
    ledger: Ledger, bad: str
) -> None:
    recorded = decide(ledger, refund(bad, "10.00"))
    assert recorded.result.decision is Decision.DENY
    assert recorded.state is None
    assert ledger.verify_chain() == 0


# --- the emergency stop ------------------------------------------------------------
@pytest.mark.edge
def test_stopping_refunds_is_recorded_once_and_survives_a_restart(
    ledger: Ledger, tmp_path: Path
) -> None:
    assert ledger.refunds_stopped() is False
    assert ledger.set_refunds_stopped(True, "Vikas", NOW) is True
    assert ledger.set_refunds_stopped(True, "Vikas", NOW) is False  # already stopped
    reopened = Ledger(tmp_path / "ledger.db", SECRET, policy(), ACCOUNT)
    assert reopened.refunds_stopped() is True
    events = [e["event"] for e in ledger.events("control:refunds")]
    assert events == ["refunds_stopped"]


@pytest.mark.edge
def test_resuming_when_not_stopped_records_nothing(ledger: Ledger) -> None:
    assert ledger.set_refunds_stopped(False, "Vikas", NOW) is False
    assert ledger.events("control:refunds") == []


@pytest.mark.edge
def test_a_stopped_ledger_denies_even_a_clean_small_refund(ledger: Ledger) -> None:
    ledger.set_refunds_stopped(True, "Vikas", NOW)
    recorded = decide(ledger, refund("r1", "5.00"))
    assert recorded.result.decision is Decision.DENY
    assert recorded.result.rule_id == "R16"


@pytest.mark.edge
def test_turning_refunds_back_on_by_editing_the_database_is_detected(
    ledger: Ledger, tmp_path: Path
) -> None:
    ledger.set_refunds_stopped(True, "Vikas", NOW)
    with sqlite3.connect(tmp_path / "ledger.db") as db:
        db.execute("UPDATE controls SET value = 'no' WHERE name = 'refunds_stopped'")
    with pytest.raises(AuditIntegrityError):
        ledger.verify_records()


# --- anchoring the chain head somewhere we don't control ---------------------------
@pytest.mark.edge
def test_the_chain_matches_a_head_published_earlier(ledger: Ledger) -> None:
    decide(ledger, refund("r1", "5.00"))
    head = ledger.head()
    decide(ledger, refund("r2", "6.00"))
    assert ledger.verify_against_head(head["seq"], head["entry_hash"]) > head["seq"]


@pytest.mark.edge
def test_a_full_rewrite_with_the_key_is_caught_by_a_published_head(
    ledger: Ledger, tmp_path: Path
) -> None:
    decide(ledger, refund("r1", "5.00"))
    published = ledger.head()
    # someone with the database and the audit key rebuilds a clean-looking chain
    with sqlite3.connect(tmp_path / "ledger.db") as db:
        db.execute("DELETE FROM audit")
    with ledger._transaction() as conn:
        for n in range(published["seq"] + 1):
            ledger.append_event(conn, f"fake-{n}", "decided", {"n": n}, NOW)
    assert ledger.verify_chain() > 0  # the rewritten chain is internally valid
    with pytest.raises(AuditIntegrityError):
        ledger.verify_against_head(published["seq"], published["entry_hash"])


@pytest.mark.edge
def test_a_chain_shorter_than_the_published_head_is_caught(ledger: Ledger) -> None:
    decide(ledger, refund("r1", "5.00"))
    head = ledger.head()
    with pytest.raises(AuditIntegrityError):
        ledger.verify_against_head(head["seq"] + 5, head["entry_hash"])


def test_an_empty_chain_has_a_genesis_head(ledger: Ledger) -> None:
    assert ledger.head() == {"seq": 0, "entry_hash": "0" * 64, "created_at": ""}


@pytest.mark.edge
def test_nothing_moves_into_sending_while_refunds_are_stopped(ledger: Ledger) -> None:
    recorded = decide(ledger, refund("r1", "30.00"))
    assert recorded.state is ProposalState.PENDING_APPROVAL
    ledger.set_refunds_stopped(True, "Vikas", NOW)
    with pytest.raises(FailClosedError):
        ledger.transition("r1", ProposalState.EXECUTING, NOW, decide_within=WINDOW)
    assert ledger.state_of("r1") is ProposalState.PENDING_APPROVAL


# --- an approval re-decides the request from fresh facts ----------
APPROVER = {"assurance": "passkey", "approver": "Vikas", "passkey": "p" * 16}


def approve(ledger: Ledger, request_id: str, at, ev: OrderEvidence | None = None):  # noqa: ANN001, ANN201
    fresh = replace_time(ev or evidence(), at)
    return ledger.approve(
        request_id, fresh, Signals(), SCREEN, at, APPROVER, decide_within=WINDOW
    )


@pytest.mark.edge
@pytest.mark.parametrize(
    ("seconds", "accepted"), [(WINDOW - 1, True), (WINDOW, False), (WINDOW + 1, False)]
)
def test_an_approval_re_checked_with_paypal_still_respects_the_deadline(
    ledger: Ledger, seconds: int, accepted: bool
) -> None:
    decide(ledger, refund("r1", "60.00"))
    at = NOW + timedelta(seconds=seconds)
    if accepted:
        assert approve(ledger, "r1", at).state is ProposalState.EXECUTING
        return
    with pytest.raises(FailClosedError):
        approve(ledger, "r1", at)
    assert ledger.state_of("r1") is ProposalState.PENDING_APPROVAL


@pytest.mark.edge
def test_an_approval_needs_its_deadline(ledger: Ledger) -> None:
    decide(ledger, refund("r1", "60.00"))
    with pytest.raises(FailClosedError):
        ledger.approve(
            "r1", evidence(), Signals(), SCREEN, NOW, APPROVER, decide_within=None
        )


@pytest.mark.edge
def test_a_refund_that_no_longer_fits_is_refused_at_approval(ledger: Ledger) -> None:
    # 100.00 paid; 60.00 asked; 50.00 refunded in PayPal directly while it waited
    decide(ledger, refund("r1", "60.00"))
    at = NOW + timedelta(seconds=60)
    recorded = approve(ledger, "r1", at, evidence(refunded="50.00"))
    assert recorded.state is ProposalState.DENIED
    assert recorded.result.rule_id == "R4"
    assert ledger.state_of("r1") is ProposalState.DENIED


@pytest.mark.edge
def test_a_changed_order_that_still_fits_goes_back_to_the_owner(ledger: Ledger) -> None:
    # 100.00 paid; 60.00 asked; 20.00 refunded in PayPal directly: it still fits,
    # but the owner approved figures that are not true any more
    decide(ledger, refund("r1", "60.00"))
    at = NOW + timedelta(seconds=60)
    recorded = approve(ledger, "r1", at, evidence(refunded="20.00"))
    assert recorded.state is ProposalState.PENDING_APPROVAL
    timing = ledger.timing("r1")
    assert timing is not None and timing[2] > NOW.isoformat()  # a fresh window
    again = approve(
        ledger, "r1", at + timedelta(seconds=30), evidence(refunded="20.00")
    )
    assert again.state is ProposalState.EXECUTING


@pytest.mark.edge
def test_a_lowered_daily_cap_refuses_at_approval(tmp_path: Path) -> None:
    first = Ledger(tmp_path / "ledger.db", SECRET, policy(), ACCOUNT)
    for n in range(4):
        ev = evidence(order=f"ORD-{n}", captured="500.00")
        first.decide(refund(f"r{n}", "250.00", f"ORD-{n}"), ev, Signals(), SCREEN, NOW)
    # the owner lowers the daily cap while the requests wait
    lowered = Ledger(
        tmp_path / "ledger.db", SECRET, policy(daily_outflow_cap="500.00"), ACCOUNT
    )
    at = NOW + timedelta(seconds=60)
    recorded = approve(lowered, "r0", at, evidence(order="ORD-0", captured="500.00"))
    assert recorded.state is ProposalState.DENIED
    assert recorded.result.rule_id == "R10"


@pytest.mark.edge
def test_a_customer_over_the_repeat_limit_goes_on_hold_at_approval(
    tmp_path: Path,
) -> None:
    first = Ledger(tmp_path / "ledger.db", SECRET, policy(), ACCOUNT)
    for n in range(3):
        ev = evidence(order=f"ORD-{n}", captured="500.00")
        first.decide(refund(f"r{n}", "60.00", f"ORD-{n}"), ev, Signals(), SCREEN, NOW)
    # the owner lowers the repeat limit while the requests wait
    lowered = Ledger(
        tmp_path / "ledger.db", SECRET, policy(velocity_max_refunds_24h=1), ACCOUNT
    )
    at = NOW + timedelta(seconds=60)
    recorded = approve(lowered, "r2", at, evidence(order="ORD-2", captured="500.00"))
    assert recorded.state is ProposalState.PAUSED
    assert recorded.result.release_at is not None


@pytest.mark.edge
def test_an_approval_with_another_orders_facts_is_refused(ledger: Ledger) -> None:
    decide(ledger, refund("r1", "60.00"))
    with pytest.raises(FailClosedError):
        approve(ledger, "r1", NOW + timedelta(seconds=60), evidence(order="ORD-9"))
    assert ledger.state_of("r1") is ProposalState.PENDING_APPROVAL


@pytest.mark.edge
def test_the_second_of_two_approvals_changes_nothing(ledger: Ledger) -> None:
    decide(ledger, refund("r1", "60.00"))
    at = NOW + timedelta(seconds=60)
    assert approve(ledger, "r1", at).state is ProposalState.EXECUTING
    with pytest.raises(FailClosedError):
        approve(ledger, "r1", at)


@pytest.mark.edge
def test_nothing_is_approved_while_refunds_are_stopped(ledger: Ledger) -> None:
    decide(ledger, refund("r1", "60.00"))
    ledger.set_refunds_stopped(True, "Vikas", NOW)
    with pytest.raises(FailClosedError):
        approve(ledger, "r1", NOW + timedelta(seconds=60))
    assert ledger.state_of("r1") is ProposalState.PENDING_APPROVAL


@pytest.mark.edge
def test_a_changed_captured_amount_is_refused_even_when_the_refund_fits(
    ledger: Ledger,
) -> None:
    # decided on 100.00 captured; PayPal now shows 90.00: the order itself changed
    decide(ledger, refund("r1", "60.00"))
    recorded = approve(
        ledger, "r1", NOW + timedelta(seconds=60), evidence(captured="90.00")
    )
    assert recorded.state is ProposalState.DENIED
    assert recorded.result.rule_id == "R14"


# --- the automatic budget counts only refunds that went out without a person ------
def _auto(ledger: Ledger, request_id: str, amount: str, order: str = "ORD-1"):  # noqa: ANN201
    return decide(ledger, refund(request_id, amount, order), evidence(order=order))


@pytest.mark.edge
def test_a_second_small_refund_on_an_order_asks_the_owner(ledger: Ledger) -> None:
    assert _auto(ledger, "r1", "24.99").state is ProposalState.EXECUTING
    second = _auto(ledger, "r2", "24.99")
    assert second.result.decision is Decision.ESCALATE
    assert second.result.rule_id == "R17"


@pytest.mark.edge
def test_small_refunds_on_two_orders_share_the_customers_budget(ledger: Ledger) -> None:
    assert _auto(ledger, "r1", "20.00", "ORD-1").state is ProposalState.EXECUTING
    other = _auto(ledger, "r2", "10.00", "ORD-2")
    assert other.result.rule_id == "R17"


@pytest.mark.edge
def test_a_refund_that_failed_at_paypal_frees_its_budget(ledger: Ledger) -> None:
    _auto(ledger, "r1", "20.00")
    ledger.transition("r1", ProposalState.FAILED, NOW)
    assert _auto(ledger, "r2", "20.00").state is ProposalState.EXECUTING


@pytest.mark.edge
def test_a_refund_the_owner_approved_does_not_use_the_automatic_budget(
    ledger: Ledger,
) -> None:
    decide(ledger, refund("r1", "60.00"))  # asks the owner
    approve(ledger, "r1", NOW + timedelta(seconds=5))
    assert ledger.state_of("r1") is ProposalState.EXECUTING
    assert _auto(ledger, "r2", "20.00").state is ProposalState.EXECUTING


@pytest.mark.edge
@pytest.mark.parametrize(
    ("seconds_ago", "counted"),
    [
        (23 * 3600, True),
        (24 * 3600, True),  # the window includes its start, like the repeat rule
        (24 * 3600 + 1, False),
    ],
)
def test_the_customers_automatic_budget_looks_back_one_day(
    ledger: Ledger, seconds_ago: int, counted: bool
) -> None:
    then = NOW - timedelta(seconds=seconds_ago)
    ledger.decide(
        refund("r1", "20.00", "ORD-1"),
        replace_time(evidence(order="ORD-1"), then),
        Signals(),
        SCREEN,
        then,
    )
    later = _auto(ledger, "r2", "10.00", "ORD-2")
    assert (later.result.rule_id == "R17") is counted


@pytest.mark.edge
def test_a_repeat_of_the_same_request_is_not_counted_against_itself(
    ledger: Ledger,
) -> None:
    first = _auto(ledger, "r1", "20.00")
    again = _auto(ledger, "r1", "20.00")  # same id, same body: replayed
    assert again.replayed and again.state is first.state
