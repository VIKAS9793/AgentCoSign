"""Approver passkeys (WebAuthn): enrolment, and approvals bound to the exact action.

The approver signs every approval with a passkey on their own device, verified
with biometrics or a device PIN. The challenge they sign is derived from the
exact recorded action (request, amount, order, state) plus a one-time random
value, so a signature approves that action only, once, and becomes useless if
anything about the action changes. Challenges expire after a short time and are
consumed on the first verification attempt, whether it succeeds or not.

Only public keys are stored. The private key never leaves the approver's device.
"""

import hashlib
import json
import secrets
import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from webauthn import (
    generate_authentication_options,
    generate_registration_options,
    options_to_json,
    verify_authentication_response,
    verify_registration_response,
)
from webauthn.helpers import base64url_to_bytes, bytes_to_base64url
from webauthn.helpers.exceptions import WebAuthnException
from webauthn.helpers.structs import (
    AuthenticatorSelectionCriteria,
    PublicKeyCredentialDescriptor,
    ResidentKeyRequirement,
    UserVerificationRequirement,
)

from agentcosign.core.exceptions import FailClosedError

CHALLENGE_TTL_SECONDS = 120
BUSY_TIMEOUT_SECONDS = 5
APPROVER_NAME_MAX = 64
INTENTS = frozenset({"approve", "reject", "view"})

_SCHEMA = """
CREATE TABLE IF NOT EXISTS passkeys (
    credential_id TEXT PRIMARY KEY,
    public_key TEXT NOT NULL,
    sign_count INTEGER NOT NULL,
    approver TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS passkey_challenges (
    purpose TEXT PRIMARY KEY,
    challenge TEXT NOT NULL,
    expires_at TEXT NOT NULL
);
"""
_Q_PUT_CHALLENGE = (
    "INSERT OR REPLACE INTO passkey_challenges (purpose, challenge, expires_at) "
    "VALUES (?, ?, ?)"
)
_Q_TAKE_CHALLENGE = (
    "SELECT challenge, expires_at FROM passkey_challenges WHERE purpose = ?"
)
_Q_DROP_CHALLENGE = "DELETE FROM passkey_challenges WHERE purpose = ?"
_Q_ADD_PASSKEY = (
    "INSERT INTO passkeys (credential_id, public_key, sign_count, approver, "
    "created_at) VALUES (?, ?, ?, ?, ?)"
)
_Q_GET_PASSKEY = (
    "SELECT public_key, sign_count, approver FROM passkeys WHERE credential_id = ?"
)
_Q_ALL_IDS = "SELECT credential_id FROM passkeys"
_Q_SET_COUNT = "UPDATE passkeys SET sign_count = ? WHERE credential_id = ?"
_Q_COUNT = "SELECT COUNT(*) FROM passkeys"

_VERIFY_ERRORS = (WebAuthnException, ValueError, KeyError, TypeError)


@dataclass(frozen=True, slots=True)
class Approver:
    """Who approved: the enrolled name and a digest of the passkey used."""

    name: str
    credential_digest: str


def _iso(moment: datetime) -> str:
    if moment.tzinfo is None:
        raise FailClosedError("passkey timestamps must carry a time zone")
    return moment.astimezone(UTC).isoformat(timespec="microseconds")


def _digest(credential_id: str) -> str:
    return hashlib.sha256(credential_id.encode("utf-8")).hexdigest()[:16]


def bound_challenge(intent: str, action_digest: str, nonce: str) -> bytes:
    """The bytes an approver signs: tied to the intent, the action and a nonce."""
    return hashlib.sha256(f"{intent}|{action_digest}|{nonce}".encode()).digest()


class PasskeyRegistry:
    """Stores approver passkeys and verifies registrations and approvals."""

    def __init__(self, path: Path | str, rp_id: str, origin: str) -> None:
        self._path = str(path)
        self._rp_id = rp_id
        self._origin = origin
        with self._connection() as conn:
            conn.executescript(_SCHEMA)

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

    def _put_challenge(self, purpose: str, value: str, now: datetime) -> None:
        expires = _iso(now + timedelta(seconds=CHALLENGE_TTL_SECONDS))
        with self._connection() as conn:
            conn.execute(_Q_PUT_CHALLENGE, (purpose, value, expires))

    def _take_challenge(self, purpose: str, now: datetime) -> str:
        """Fetch and delete a challenge; it can be used for one attempt only."""
        with self._connection() as conn:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute(_Q_TAKE_CHALLENGE, (purpose,)).fetchone()
            conn.execute(_Q_DROP_CHALLENGE, (purpose,))
            conn.execute("COMMIT")
        if row is None or row["expires_at"] < _iso(now):
            raise FailClosedError("the passkey request expired or was already used")
        value: str = row["challenge"]
        return value

    def has_passkeys(self) -> bool:
        """True once at least one approver has enrolled."""
        with self._connection() as conn:
            return int(conn.execute(_Q_COUNT).fetchone()[0]) > 0

    # --------------------------------------------------------------- enrolment

    def registration_options(self, approver: str, now: datetime) -> str:
        """Options for the browser to create a new approver passkey (JSON)."""
        name = approver.strip()
        if not name or len(name) > APPROVER_NAME_MAX:
            raise FailClosedError("approver name is missing or too long")
        options = generate_registration_options(
            rp_id=self._rp_id,
            rp_name="AgentCoSign",
            user_name=name,
            user_id=hashlib.sha256(name.encode("utf-8")).digest(),
            authenticator_selection=AuthenticatorSelectionCriteria(
                resident_key=ResidentKeyRequirement.PREFERRED,
                user_verification=UserVerificationRequirement.REQUIRED,
            ),
            exclude_credentials=self._descriptors(),
        )
        challenge = bytes_to_base64url(options.challenge)
        self._put_challenge(f"register:{name}", challenge, now)
        return options_to_json(options)

    def register(self, approver: str, credential: dict[str, Any], now: datetime) -> str:
        """Verify the browser's new passkey and store its public key."""
        name = approver.strip()
        expected = base64url_to_bytes(self._take_challenge(f"register:{name}", now))
        try:
            verified = verify_registration_response(
                credential=credential,
                expected_challenge=expected,
                expected_rp_id=self._rp_id,
                expected_origin=self._origin,
                require_user_verification=True,
            )
        except _VERIFY_ERRORS as exc:
            raise FailClosedError("the passkey could not be verified") from exc
        credential_id = bytes_to_base64url(verified.credential_id)
        with self._connection() as conn:
            conn.execute(
                _Q_ADD_PASSKEY,
                (
                    credential_id,
                    bytes_to_base64url(verified.credential_public_key),
                    verified.sign_count,
                    name,
                    _iso(now),
                ),
            )
        return credential_id

    def _descriptors(self) -> list[PublicKeyCredentialDescriptor]:
        with self._connection() as conn:
            rows = conn.execute(_Q_ALL_IDS).fetchall()
        return [
            PublicKeyCredentialDescriptor(id=base64url_to_bytes(row["credential_id"]))
            for row in rows
        ]

    # --------------------------------------------------------------- approvals

    def approval_options(
        self, request_id: str, intent: str, action_digest: str, now: datetime
    ) -> str:
        """Options for signing one decision about one exact action (JSON)."""
        if intent not in INTENTS:
            raise FailClosedError("unknown approval intent")
        descriptors = self._descriptors()
        if not descriptors:
            raise FailClosedError("no approver passkey is enrolled")
        nonce = secrets.token_hex(16)
        options = generate_authentication_options(
            rp_id=self._rp_id,
            challenge=bound_challenge(intent, action_digest, nonce),
            allow_credentials=descriptors,
            user_verification=UserVerificationRequirement.REQUIRED,
        )
        self._put_challenge(f"{intent}:{request_id}", nonce, now)
        return options_to_json(options)

    def verify_approval(
        self,
        request_id: str,
        intent: str,
        action_digest: str,
        credential: dict[str, Any],
        now: datetime,
    ) -> Approver:
        """Verify the signed decision; the action must be unchanged since issued."""
        nonce = self._take_challenge(f"{intent}:{request_id}", now)
        credential_id = str(credential.get("id", ""))
        with self._connection() as conn:
            row = conn.execute(_Q_GET_PASSKEY, (credential_id,)).fetchone()
        if row is None:
            raise FailClosedError("this passkey is not enrolled")
        try:
            verified = verify_authentication_response(
                credential=credential,
                expected_challenge=bound_challenge(intent, action_digest, nonce),
                expected_rp_id=self._rp_id,
                expected_origin=self._origin,
                credential_public_key=base64url_to_bytes(row["public_key"]),
                credential_current_sign_count=int(row["sign_count"]),
                require_user_verification=True,
            )
        except _VERIFY_ERRORS as exc:
            raise FailClosedError("the approval signature is not valid") from exc
        with self._connection() as conn:
            conn.execute(_Q_SET_COUNT, (verified.new_sign_count, credential_id))
        return Approver(
            name=str(row["approver"]), credential_digest=_digest(credential_id)
        )


def credential_from_json(text: str) -> dict[str, Any]:
    """Parse a browser credential sent as JSON text into a dictionary."""
    try:
        value = json.loads(text)
    except ValueError as exc:
        raise FailClosedError("the passkey response is not valid JSON") from exc
    if not isinstance(value, dict):
        raise FailClosedError("the passkey response is not an object")
    return value
