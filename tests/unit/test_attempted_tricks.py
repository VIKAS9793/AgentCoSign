"""Messages that tried to trick the assistant: kept in the signed record and
listed for the merchant even though no refund was ever proposed.

Edge cases (written before the code):

| Case | Expected |
|---|---|
| A caught message is recorded | One entry: id, digest, guard, verdict; no words |
| Entry exactly 24 hours old | Still listed (the same boundary as refunds) |
| Entry older than 24 hours | Not listed |
| More than the list holds | The newest ones, newest first |
| The entry is edited in the database | The full record check fails |
| Either check alone finds an attack | Counted as an attack; unknown or clean is not |
"""

import sqlite3
from datetime import timedelta
from pathlib import Path

import pytest

from agentcosign.core.exceptions import AuditIntegrityError
from agentcosign.models.policy import ScreeningVerdict, Signals
from agentcosign.services.ledger import RECENT_LIMIT, Ledger
from agentcosign.services.policy_engine import looks_like_attack
from tests.scenarios.policy_scenarios import ACCOUNT, NOW, policy

SECRET = b"s" * 32


@pytest.fixture
def ledger(tmp_path: Path) -> Ledger:
    return Ledger(tmp_path / "ledger.db", SECRET, policy(), ACCOUNT)


def _body(message_id: str = "msg-1") -> dict[str, str]:
    return {
        "message_id": message_id,
        "message_digest": "d" * 64,
        "prompt_guard": "0.9700",
        "screening": "clean",
    }


def test_a_caught_message_is_one_entry_without_its_words(ledger: Ledger) -> None:
    ledger.record_attack(_body(), NOW)
    caught = ledger.attacks_since(NOW)
    assert len(caught) == 1
    entry = caught[0]
    assert entry["event"] == "attack_caught"
    assert set(entry) == {"at", "event", *_body()}
    assert ledger.verify_chain() == 1


@pytest.mark.edge
def test_the_list_keeps_exactly_one_day(ledger: Ledger) -> None:
    ledger.record_attack(_body("msg-old"), NOW - timedelta(hours=24, seconds=1))
    ledger.record_attack(_body("msg-edge"), NOW - timedelta(hours=24))
    ledger.record_attack(_body("msg-new"), NOW)
    listed = [entry["message_id"] for entry in ledger.attacks_since(NOW)]
    assert listed == ["msg-new", "msg-edge"]


@pytest.mark.edge
def test_the_list_holds_the_newest_first(ledger: Ledger) -> None:
    for n in range(RECENT_LIMIT + 3):
        ledger.record_attack(_body(f"msg-{n}"), NOW + timedelta(seconds=n))
    later = NOW + timedelta(seconds=RECENT_LIMIT + 3)
    listed = [entry["message_id"] for entry in ledger.attacks_since(later)]
    assert len(listed) == RECENT_LIMIT
    assert listed[0] == f"msg-{RECENT_LIMIT + 2}"


@pytest.mark.edge
def test_editing_a_caught_entry_is_detected(ledger: Ledger, tmp_path: Path) -> None:
    ledger.record_attack(_body(), NOW)
    with sqlite3.connect(tmp_path / "ledger.db") as conn:
        conn.execute("UPDATE audit SET body = replace(body, '0.9700', '0.0100')")
    with pytest.raises(AuditIntegrityError):
        ledger.verify_records()


@pytest.mark.edge
@pytest.mark.parametrize(
    ("signals", "attack"),
    [
        (Signals(screening=ScreeningVerdict.INJECTION), True),
        (Signals(screening=ScreeningVerdict.CLEAN, prescreen_hit=True), True),
        (Signals(screening=ScreeningVerdict.CLEAN), False),
        (Signals(screening=ScreeningVerdict.UNAVAILABLE), False),
    ],
)
def test_either_check_alone_counts_as_an_attack(signals: Signals, attack: bool) -> None:
    assert looks_like_attack(signals) is attack
