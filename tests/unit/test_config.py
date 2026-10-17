"""Unit tests for configuration loading and fail-closed validation."""

from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest
import yaml
from pydantic import ValidationError

from agentcosign.core.config import (
    PolicySettings,
    Settings,
    _load_yaml_config,
    get_settings,
)
from agentcosign.core.exceptions import ConfigurationError

VALID_POLICY: dict[str, Any] = {
    "currency": "USD",
    "refund_auto_limit": "25.00",
    "refund_max_limit": "250.00",
    "velocity_max_refunds_24h": 3,
    "payout_new_payee_action": "escalate",
    "payout_duplicate_window_days": 30,
    "daily_outflow_cap": "1000.00",
    "evidence_max_age_seconds": 60,
}


def test_settings_load_from_yaml() -> None:
    settings = get_settings()
    assert settings.policy.currency == "USD"
    assert settings.policy.refund_auto_limit == Decimal("25.00")
    assert settings.policy.refund_max_limit == Decimal("250.00")
    assert settings.policy.daily_outflow_cap == Decimal("1000.00")


def _approval(open_seconds: int, fingerprint_seconds: int) -> object:
    from agentcosign.core.config import ApprovalSettings

    return ApprovalSettings(
        open_seconds=open_seconds,
        fingerprint_seconds=fingerprint_seconds,
        session_seconds=600,
        enrolment_link_seconds=600,
    )


def test_requests_stay_open_four_hours_and_prompts_two_minutes() -> None:
    approval = get_settings().approval
    assert approval.open_seconds == 14_400
    assert approval.fingerprint_seconds == 120


@pytest.mark.edge
@pytest.mark.parametrize(
    ("open_seconds", "fingerprint_seconds", "accepted"),
    [
        (86_400, 300, True),  # both exactly at their caps
        (86_401, 120, False),  # open window one second over a day
        (14_400, 301, False),  # fingerprint prompt one second over five minutes
        (299, 120, False),  # open window too short to be answered
        (14_400, 29, False),  # prompt too short for a fingerprint
    ],
)
def test_configuration_cannot_stretch_the_approval_windows(
    open_seconds: int, fingerprint_seconds: int, accepted: bool
) -> None:
    """The caps live in code, so editing the config file cannot raise them."""
    if accepted:
        _approval(open_seconds, fingerprint_seconds)
        return
    with pytest.raises(ValidationError):
        _approval(open_seconds, fingerprint_seconds)


def test_money_is_decimal_not_float() -> None:
    settings = get_settings()
    for value in (
        settings.policy.refund_auto_limit,
        settings.policy.refund_max_limit,
        settings.policy.daily_outflow_cap,
    ):
        assert isinstance(value, Decimal)


def test_models_config_loaded() -> None:
    settings = get_settings()
    assert settings.models.agent.provider == "groq"
    assert settings.models.agent.primary == "openai/gpt-oss-120b"
    assert settings.models.screening.primary == "openai/gpt-oss-safeguard-20b"
    assert settings.models.extraction.provider == "nvidia_nim"


def test_secure_hermetic_defaults() -> None:
    """Field defaults (independent of any local .env) are safe and hermetic."""
    fields = Settings.model_fields
    assert fields["host"].default == "127.0.0.1"
    assert fields["debug"].default is False
    assert fields["replay_mode"].default is True
    assert fields["paypal_execution"].default == "recorded"
    assert get_settings().paypal_mode == "sandbox"


def test_production_cannot_be_selected_for_paypal() -> None:
    with pytest.raises(ValidationError):
        Settings.model_validate({"PAYPAL_MODE": "live"}, strict=False)


def test_gateway_secrets_are_never_empty() -> None:
    settings = get_settings()
    for secret in (
        settings.agent_api_key,
        settings.operator_api_key,
        settings.audit_hmac_key,
    ):
        assert len(secret.get_secret_value()) >= 32


def test_missing_config_file_aborts_startup(tmp_path: Path) -> None:
    with pytest.raises(ConfigurationError):
        _load_yaml_config(tmp_path / "nope.yaml")


def test_non_mapping_config_aborts_startup(tmp_path: Path) -> None:
    bad = tmp_path / "config.yaml"
    bad.write_text(yaml.safe_dump(["not", "a", "mapping"]), encoding="utf-8")
    with pytest.raises(ConfigurationError):
        _load_yaml_config(bad)


def test_unknown_policy_key_is_rejected() -> None:
    """A typo must not silently fall back to a default limit."""
    with pytest.raises(ValidationError):
        PolicySettings(**{**VALID_POLICY, "refund_auto_limt": "999"})


def test_missing_policy_limit_is_rejected() -> None:
    incomplete = {k: v for k, v in VALID_POLICY.items() if k != "refund_auto_limit"}
    with pytest.raises(ValidationError):
        PolicySettings(**incomplete)


@pytest.mark.parametrize(
    "override",
    [
        {"refund_auto_limit": "0"},
        {"refund_auto_limit": "300.00"},  # auto > max
        {"daily_outflow_cap": "100.00"},  # cap < max refund
        {"payout_new_payee_action": "allow"},  # never auto-allow new payees
        {"currency": "EUR"},
        {"velocity_max_refunds_24h": 0},
    ],
)
def test_nonsensical_policy_is_rejected(override: dict[str, Any]) -> None:
    with pytest.raises(ValidationError):
        PolicySettings(**{**VALID_POLICY, **override})


def test_float_yaml_numbers_do_not_leak_binary_error() -> None:
    policy = PolicySettings(**{**VALID_POLICY, "refund_auto_limit": 0.1})
    assert policy.refund_auto_limit == Decimal("0.1")
