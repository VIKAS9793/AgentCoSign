"""Approver passkeys: enrolment and approvals bound to one exact action."""

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


@pytest.fixture
def registry(tmp_path: Path) -> PasskeyRegistry:
    return PasskeyRegistry(tmp_path / "ledger.db", RP_ID, ORIGIN)


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


def test_an_expired_request_is_refused(
    registry: PasskeyRegistry, phone: SoftAuthenticator
) -> None:
    options = registry.approval_options("r1", "approve", ACTION, NOW)
    later = NOW + timedelta(seconds=121)
    with pytest.raises(FailClosedError):
        registry.verify_approval("r1", "approve", ACTION, phone.sign(options), later)


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
