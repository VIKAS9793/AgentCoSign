"""Approver passkeys: enrolment and approvals bound to one exact action."""

import json
from datetime import timedelta
from pathlib import Path

import pytest

from agentcosign.core.exceptions import FailClosedError
from agentcosign.services.passkeys import PasskeyRegistry
from tests.scenarios.policy_scenarios import NOW
from tests.unit.soft_authenticator import SoftAuthenticator

RP_ID = "localhost"
ORIGIN = "http://localhost:8000"
ACTION = "a" * 64
PROMPT_SECONDS = 120


@pytest.fixture
def registry(tmp_path: Path) -> PasskeyRegistry:
    return PasskeyRegistry(
        tmp_path / "ledger.db", RP_ID, ORIGIN, prompt_seconds=PROMPT_SECONDS
    )


@pytest.fixture
def phone(registry: PasskeyRegistry) -> SoftAuthenticator:
    device = SoftAuthenticator(RP_ID, ORIGIN)
    options = registry.registration_options("Vikas", NOW)
    registry.register("Vikas", device.register(options), NOW)
    return device


def test_enrolment_stores_a_passkey(
    registry: PasskeyRegistry, phone: SoftAuthenticator
) -> None:
    assert registry.has_passkeys()


def test_enrolment_requires_biometric_or_pin(registry: PasskeyRegistry) -> None:
    device = SoftAuthenticator(RP_ID, ORIGIN)
    options = registry.registration_options("Vikas", NOW)
    with pytest.raises(FailClosedError):
        registry.register("Vikas", device.register(options, verified=False), NOW)
    assert not registry.has_passkeys()


def test_enrolment_from_another_site_is_refused(registry: PasskeyRegistry) -> None:
    device = SoftAuthenticator(RP_ID, "https://evil.example")
    options = registry.registration_options("Vikas", NOW)
    with pytest.raises(FailClosedError):
        registry.register("Vikas", device.register(options), NOW)


def test_valid_approval_identifies_the_approver(
    registry: PasskeyRegistry, phone: SoftAuthenticator
) -> None:
    options = registry.approval_options("r1", "approve", ACTION, NOW)
    approver = registry.verify_approval(
        "r1", "approve", ACTION, phone.sign(options), NOW
    )
    assert approver.name == "Vikas"
    assert len(approver.credential_digest) == 16


def test_a_signature_cannot_be_used_twice(
    registry: PasskeyRegistry, phone: SoftAuthenticator
) -> None:
    options = registry.approval_options("r1", "approve", ACTION, NOW)
    signed = phone.sign(options)
    registry.verify_approval("r1", "approve", ACTION, signed, NOW)
    with pytest.raises(FailClosedError):
        registry.verify_approval("r1", "approve", ACTION, signed, NOW)


def test_approval_breaks_if_the_action_changed(
    registry: PasskeyRegistry, phone: SoftAuthenticator
) -> None:
    options = registry.approval_options("r1", "approve", ACTION, NOW)
    signed = phone.sign(options)
    with pytest.raises(FailClosedError):
        registry.verify_approval("r1", "approve", "b" * 64, signed, NOW)


def test_a_reject_signature_cannot_approve(
    registry: PasskeyRegistry, phone: SoftAuthenticator
) -> None:
    reject_options = registry.approval_options("r1", "reject", ACTION, NOW)
    registry.approval_options("r1", "approve", ACTION, NOW)
    signed_reject = phone.sign(reject_options)
    with pytest.raises(FailClosedError):
        registry.verify_approval("r1", "approve", ACTION, signed_reject, NOW)


def test_a_signature_for_another_request_is_refused(
    registry: PasskeyRegistry, phone: SoftAuthenticator
) -> None:
    options = registry.approval_options("r1", "approve", ACTION, NOW)
    registry.approval_options("r2", "approve", ACTION, NOW)
    with pytest.raises(FailClosedError):
        registry.verify_approval("r2", "approve", ACTION, phone.sign(options), NOW)


@pytest.mark.edge
@pytest.mark.parametrize(
    ("seconds", "accepted"),
    [(PROMPT_SECONDS - 1, True), (PROMPT_SECONDS, False), (PROMPT_SECONDS + 1, False)],
)
def test_a_fingerprint_prompt_works_only_inside_its_window(
    registry: PasskeyRegistry, phone: SoftAuthenticator, seconds: int, accepted: bool
) -> None:
    """The prompt closes at its last moment, like the request's own deadline."""
    options = registry.approval_options("r1", "approve", ACTION, NOW)
    signed = phone.sign(options)
    at = NOW + timedelta(seconds=seconds)
    if accepted:
        assert registry.verify_approval("r1", "approve", ACTION, signed, at).name
        return
    with pytest.raises(FailClosedError):
        registry.verify_approval("r1", "approve", ACTION, signed, at)


@pytest.mark.edge
def test_the_phone_is_told_the_same_prompt_window(registry: PasskeyRegistry) -> None:
    """The browser gives up when the server does, never earlier or later."""
    device = SoftAuthenticator(RP_ID, ORIGIN)
    enrol = json.loads(registry.registration_options("Vikas", NOW))
    assert enrol["timeout"] == PROMPT_SECONDS * 1000
    registry.register("Vikas", device.register(json.dumps(enrol)), NOW)
    sign = json.loads(registry.approval_options("r1", "approve", ACTION, NOW))
    assert sign["timeout"] == PROMPT_SECONDS * 1000


def test_approval_without_biometric_or_pin_is_refused(
    registry: PasskeyRegistry, phone: SoftAuthenticator
) -> None:
    options = registry.approval_options("r1", "approve", ACTION, NOW)
    with pytest.raises(FailClosedError):
        registry.verify_approval(
            "r1", "approve", ACTION, phone.sign(options, verified=False), NOW
        )


def test_an_unenrolled_device_is_refused(
    registry: PasskeyRegistry, phone: SoftAuthenticator
) -> None:
    stranger = SoftAuthenticator(RP_ID, ORIGIN, credential_id=b"other")
    options = registry.approval_options("r1", "approve", ACTION, NOW)
    with pytest.raises(FailClosedError):
        registry.verify_approval("r1", "approve", ACTION, stranger.sign(options), NOW)


def test_a_cloned_passkey_replaying_an_old_counter_is_refused(
    registry: PasskeyRegistry, phone: SoftAuthenticator
) -> None:
    first = registry.approval_options("r1", "approve", ACTION, NOW)
    registry.verify_approval("r1", "approve", ACTION, phone.sign(first), NOW)
    phone.sign_count = 0  # a clone that does not know the real counter
    second = registry.approval_options("r2", "approve", ACTION, NOW)
    with pytest.raises(FailClosedError):
        registry.verify_approval("r2", "approve", ACTION, phone.sign(second), NOW)


def test_approval_needs_an_enrolled_approver(registry: PasskeyRegistry) -> None:
    with pytest.raises(FailClosedError):
        registry.approval_options("r1", "approve", ACTION, NOW)


def test_unknown_intent_is_refused(
    registry: PasskeyRegistry, phone: SoftAuthenticator
) -> None:
    with pytest.raises(FailClosedError):
        registry.approval_options("r1", "execute", ACTION, NOW)


# --- revoking a lost or replaced phone ---------------------------------------------
@pytest.mark.edge
def test_a_revoked_passkey_cannot_sign_even_with_options_issued_before(
    registry: PasskeyRegistry, phone: SoftAuthenticator
) -> None:
    options = registry.approval_options("r1", "approve", ACTION, NOW)
    assert registry.revoke("Vikas", NOW) == 1
    with pytest.raises(FailClosedError):
        registry.verify_approval("r1", "approve", ACTION, phone.sign(options), NOW)
    assert not registry.has_passkeys()  # the last one gone: enrol again


@pytest.mark.edge
def test_revoking_an_unknown_name_changes_nothing(
    registry: PasskeyRegistry, phone: SoftAuthenticator
) -> None:
    assert registry.revoke("Someone Else", NOW) == 0
    assert registry.has_passkeys()


@pytest.mark.edge
def test_an_older_database_gains_revocation_without_losing_passkeys(
    tmp_path: Path,
) -> None:
    import sqlite3

    path = tmp_path / "old.db"
    with sqlite3.connect(path) as db:
        db.execute(
            "CREATE TABLE passkeys (credential_id TEXT PRIMARY KEY, public_key TEXT "
            "NOT NULL, sign_count INTEGER NOT NULL, approver TEXT NOT NULL, "
            "created_at TEXT NOT NULL)"
        )
        db.execute("INSERT INTO passkeys VALUES ('id-1', 'key', 0, 'Vikas', 'now')")
    registry = PasskeyRegistry(path, RP_ID, ORIGIN, prompt_seconds=PROMPT_SECONDS)
    assert registry.has_passkeys()
    assert registry.revoke("Vikas", NOW) == 1
    assert not registry.has_passkeys()
