"""Customer messages, masked before they are kept, and the AI checks run on them.

A message is masked and capped on arrival, so card, identity and contact
details never reach the database or a model. The checks for a request are kept
with it, so every later look at that request (approval, release from hold)
decides on the same reading instead of asking the models again.
"""

import hashlib
import hmac
import json
import secrets
import sqlite3
from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from pathlib import Path
from typing import Any

from agentcosign.core import trace
from agentcosign.models.policy import (
    CustomerReading,
    ReadingStatus,
    ScreeningVerdict,
    Signals,
)
from agentcosign.services.masking import mask

BUSY_TIMEOUT_SECONDS = 10
_SCHEMA = (
    "CREATE TABLE IF NOT EXISTS messages (message_id TEXT PRIMARY KEY, "
    "masked_text TEXT NOT NULL, digest TEXT NOT NULL, masked_counts TEXT NOT NULL, "
    "received_at TEXT NOT NULL, order_id TEXT)",
    "CREATE TABLE IF NOT EXISTS message_checks (request_id TEXT PRIMARY KEY, "
    "message_id TEXT NOT NULL, signals_json TEXT NOT NULL, record_json TEXT NOT NULL, "
    "checked_at TEXT NOT NULL)",
)
_Q_ADD = (
    "INSERT INTO messages (message_id, masked_text, digest, masked_counts, "
    "received_at, order_id) VALUES (?, ?, ?, ?, ?, ?)"
)
_Q_COLUMNS = "SELECT name FROM pragma_table_info('messages')"
_Q_ADD_ORDER_COLUMN = "ALTER TABLE messages ADD COLUMN order_id TEXT"
_Q_ORDER = "SELECT order_id FROM messages WHERE message_id = ?"
_Q_TEXT = "SELECT masked_text FROM messages WHERE message_id = ?"
_Q_DIGEST = "SELECT digest FROM messages WHERE message_id = ?"
_Q_SAVE = (
    "INSERT OR IGNORE INTO message_checks (request_id, message_id, signals_json, "
    "record_json, checked_at) VALUES (?, ?, ?, ?, ?)"
)
_Q_CHECKS = "SELECT signals_json, record_json FROM message_checks WHERE request_id = ?"


@dataclass(frozen=True, slots=True)
class StoredChecks:
    """The signals a request was decided on and what produced them."""

    signals: Signals
    record: Mapping[str, str]


def signals_to_dict(signals: Signals) -> dict[str, Any]:
    """Signals as plain JSON values, money as text."""
    reading = signals.reading
    return {
        "screening": signals.screening.value,
        "prescreen_hit": signals.prescreen_hit,
        "reading_status": signals.reading_status.value,
        "message_order": signals.message_order,
        "reading": None
        if reading is None
        else {
            "order_id": reading.order_id,
            "amount": None if reading.amount is None else str(reading.amount),
            "currency": reading.currency,
            "reason": reading.reason,
            "wants_other_destination": reading.wants_other_destination,
        },
    }


def signals_from_dict(data: Mapping[str, Any]) -> Signals:
    """Rebuild signals saved by ``signals_to_dict``."""
    raw = data.get("reading")
    reading = None
    if raw is not None:
        amount = raw.get("amount")
        reading = CustomerReading(
            order_id=raw.get("order_id"),
            amount=None if amount is None else Decimal(amount),
            currency=raw.get("currency"),
            reason=raw.get("reason"),
            wants_other_destination=bool(raw.get("wants_other_destination")),
        )
    return Signals(
        screening=ScreeningVerdict(data["screening"]),
        prescreen_hit=bool(data["prescreen_hit"]),
        reading_status=ReadingStatus(data["reading_status"]),
        reading=reading,
        message_order=data.get("message_order"),
    )


class MessageStore:
    """Masked customer messages and the checks decided on, in the shared database."""

    def __init__(self, path: Path, secret: bytes, limit: int) -> None:
        self._path = path
        self._secret = secret
        self._limit = limit
        with self._connection() as conn:
            for statement in _SCHEMA:
                conn.execute(statement)
            columns = {str(row["name"]) for row in conn.execute(_Q_COLUMNS)}
            if "order_id" not in columns:  # a database from before messages had one
                conn.execute(_Q_ADD_ORDER_COLUMN)

    @contextmanager
    def _connection(self) -> Iterator[sqlite3.Connection]:
        conn = sqlite3.connect(
            self._path, timeout=BUSY_TIMEOUT_SECONDS, isolation_level=None
        )
        conn.row_factory = sqlite3.Row
        try:
            yield conn
        finally:
            conn.close()

    def add(self, text: str, now: datetime, order_id: str | None = None) -> str:
        """Mask, cap and keep a customer's message; returns its id.

        ``order_id`` is the order the message came in on, known from the intake
        itself. It is what ties a message to one order, so it must never be read
        out of the message's own words.
        """
        masked = mask(text, self._limit)
        digest = hmac.new(
            self._secret, masked.text.encode("utf-8"), hashlib.sha256
        ).hexdigest()
        message_id = f"msg-{secrets.token_hex(8)}"
        counts = json.dumps(dict(masked.counts), sort_keys=True)
        with self._connection() as conn:
            conn.execute(
                _Q_ADD,
                (message_id, masked.text, digest, counts, now.isoformat(), order_id),
            )
        removed = ", ".join(f"{n} {kind}" for kind, n in masked.counts.items())
        trace.emit(
            "message",
            "Customer message masked and kept",
            masked=removed or "nothing to mask",
            text=masked.text,
            digest=digest[:12],
        )
        return message_id

    def text(self, message_id: str) -> str | None:
        """The masked message, or None if no such message is kept."""
        with self._connection() as conn:
            row = conn.execute(_Q_TEXT, (message_id,)).fetchone()
        return None if row is None else str(row["masked_text"])

    def order_of(self, message_id: str) -> str | None:
        """The order a message came in on, or None when intake did not know it."""
        with self._connection() as conn:
            row = conn.execute(_Q_ORDER, (message_id,)).fetchone()
        return None if row is None or row["order_id"] is None else str(row["order_id"])

    def digest(self, message_id: str) -> str | None:
        """The keyed digest of the masked message, or None if it is not kept."""
        with self._connection() as conn:
            row = conn.execute(_Q_DIGEST, (message_id,)).fetchone()
        return None if row is None else str(row["digest"])

    def save_checks(
        self,
        request_id: str,
        message_id: str,
        signals: Signals,
        record: Mapping[str, str],
        now: datetime,
    ) -> None:
        """Keep the checks a request was decided on; the first result stands."""
        values = (
            request_id,
            message_id,
            json.dumps(signals_to_dict(signals), sort_keys=True),
            json.dumps(dict(record), sort_keys=True),
            now.isoformat(),
        )
        with self._connection() as conn:
            conn.execute(_Q_SAVE, values)

    def checks_for(self, request_id: str) -> StoredChecks | None:
        """The checks a request was decided on, if it had a customer message."""
        with self._connection() as conn:
            row = conn.execute(_Q_CHECKS, (request_id,)).fetchone()
        if row is None:
            return None
        return StoredChecks(
            signals_from_dict(json.loads(row["signals_json"])),
            json.loads(row["record_json"]),
        )
