"""Configuration loader for AgentCoSign.

Combines config.yaml (Single Source of Truth) with environment variables.

Design rules (see docs/DECISIONS.md, ADR-006):
  * Safety invariants are not configurable; only limits are.
  * Unknown keys, missing files and nonsensical limits abort startup.
  * Money is Decimal, never float.
  * PayPal mode is pinned to sandbox: this build cannot touch real funds.
  * Defaults are hermetic and secure so a fresh clone runs with no credentials.
"""

import logging
import os
import secrets
from decimal import Decimal
from functools import lru_cache
from pathlib import Path
from typing import Any, Literal

import yaml
from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    SecretStr,
    ValidationError,
    field_validator,
    model_validator,
)
from pydantic_settings import BaseSettings, SettingsConfigDict

from agentcosign.core.exceptions import ConfigurationError

logger = logging.getLogger(__name__)

MIN_SECRET_LENGTH = 32
_STRICT = ConfigDict(extra="forbid", frozen=True)


def _to_decimal(value: Any) -> Decimal:
    """Parse money without float error ("25.00" and 25.0 both become 25.00)."""
    if isinstance(value, Decimal):
        return value
    if isinstance(value, bool) or not isinstance(value, (str, int, float)):
        raise ValueError("money value must be a string or number")
    return Decimal(str(value))


class PolicySettings(BaseModel):
    """Limits for the policy rules. Invariant rules have no setting here."""

    model_config = _STRICT

    currency: Literal["USD"]
    refund_auto_limit: Decimal  # R1 / R2 boundary
    refund_max_limit: Decimal  # R2 / R3 boundary
    velocity_max_refunds_24h: int = Field(ge=1, le=100)  # R5
    payout_new_payee_action: Literal["escalate", "deny"]  # R8: never "allow"
    payout_duplicate_window_days: int = Field(ge=1, le=365)  # R9
    daily_outflow_cap: Decimal  # R10

    @field_validator(
        "refund_auto_limit", "refund_max_limit", "daily_outflow_cap", mode="before"
    )
    @classmethod
    def _parse_money(cls, value: Any) -> Decimal:
        return _to_decimal(value)

    @model_validator(mode="after")
    def _check_limit_ordering(self) -> "PolicySettings":
        if self.refund_auto_limit <= 0:
            raise ValueError("refund_auto_limit must be > 0")
        if self.refund_max_limit < self.refund_auto_limit:
            raise ValueError("refund_max_limit must be >= refund_auto_limit")
        if self.daily_outflow_cap < self.refund_max_limit:
            raise ValueError("daily_outflow_cap must be >= refund_max_limit")
        return self


class ApprovalSettings(BaseModel):
    """Human co-sign behaviour."""

    model_config = _STRICT

    ttl_seconds: int = Field(ge=30, le=86_400)
    allowed_assurance: list[Literal["passkey", "session"]] = Field(min_length=1)


class ScreeningSettings(BaseModel):
    """Prompt-injection screening behaviour (R11)."""

    model_config = _STRICT

    on_unavailable: Literal["escalate", "deny"]  # never "allow"
    max_document_chars: int = Field(ge=500, le=50_000)


class ModelRoleConfig(BaseModel):
    """Configuration for an individual model role."""

    model_config = _STRICT

    provider: Literal["groq", "nvidia_nim"]
    primary: str = Field(min_length=1)
    fallback: str = Field(min_length=1)
    temperature: float = Field(ge=0.0, le=2.0)
    max_tokens: int = Field(ge=16, le=16_384)
    timeout_seconds: int = Field(ge=1, le=120)
    base_url: str | None = None


class ModelsConfig(BaseModel):
    """Configuration for all model tiers."""

    model_config = _STRICT

    agent: ModelRoleConfig
    screening: ModelRoleConfig
    extraction: ModelRoleConfig


class AuditConfig(BaseModel):
    """Configuration for audit logging and hash chaining."""

    model_config = _STRICT

    hash_algorithm: Literal["sha256"]
    storage_type: Literal["sqlite"]
    table_name: str = Field(pattern=r"^[a-z_][a-z0-9_]{0,62}$")


class Settings(BaseSettings):
    """Root application settings combining environment variables and YAML config."""

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    # Runtime. Secure, hermetic defaults: loopback bind, debug off, no live calls.
    env: Literal["development", "test", "demo", "production"] = Field(
        default="development", alias="AGENTCOSIGN_ENV"
    )
    host: str = Field(default="127.0.0.1", alias="AGENTCOSIGN_HOST")
    port: int = Field(default=8000, ge=1, le=65_535, alias="AGENTCOSIGN_PORT")
    debug: bool = Field(default=False, alias="AGENTCOSIGN_DEBUG")

    # Degradation switches (docs/ARCHITECTURE.md, "Run modes").
    #   replay_mode=True      LLM answers come from the recorded cache
    #   paypal_execution      "recorded" replays real sandbox evidence;
    #                         "sandbox" calls the live PayPal sandbox
    replay_mode: bool = Field(default=True, alias="AGENTCOSIGN_REPLAY_MODE")
    paypal_execution: Literal["recorded", "sandbox"] = Field(
        default="recorded", alias="AGENTCOSIGN_PAYPAL_EXECUTION"
    )

    # PayPal. Pinned to sandbox; there is no way to select production.
    paypal_mode: Literal["sandbox"] = Field(default="sandbox", alias="PAYPAL_MODE")
    paypal_client_id: str = Field(default="", alias="PAYPAL_CLIENT_ID")
    paypal_client_secret: SecretStr = Field(
        default=SecretStr(""), alias="PAYPAL_CLIENT_SECRET"
    )
    paypal_webhook_id: str = Field(default="", alias="PAYPAL_WEBHOOK_ID")

    # LLM providers
    groq_api_key: SecretStr = Field(default=SecretStr(""), alias="GROQ_API_KEY")
    nvidia_nim_api_key: SecretStr = Field(
        default=SecretStr(""), alias="NVIDIA_NIM_API_KEY"
    )

    # Gateway credentials: three separate principals (maker / checker / auditor).
    agent_api_key: SecretStr = Field(
        default=SecretStr(""), alias="AGENTCOSIGN_AGENT_KEY"
    )
    operator_api_key: SecretStr = Field(
        default=SecretStr(""), alias="AGENTCOSIGN_OPERATOR_KEY"
    )
    audit_hmac_key: SecretStr = Field(
        default=SecretStr(""), alias="AGENTCOSIGN_AUDIT_KEY"
    )

    # Database
    sqlite_db_path: str = Field(
        default="data/agentcosign_audit.db", alias="SQLITE_DB_PATH"
    )

    # Loaded from config.yaml (required: no silent defaults for safety limits)
    policy: PolicySettings
    approval: ApprovalSettings
    screening: ScreeningSettings
    models: ModelsConfig
    audit: AuditConfig

    @model_validator(mode="after")
    def _enforce_credential_hygiene(self) -> "Settings":
        """Demo/production refuse weak or missing gateway secrets.

        Development/test generate throwaway per-process secrets so a fresh clone
        runs with zero setup, but those secrets never survive a restart.
        """
        for name in ("agent_api_key", "operator_api_key", "audit_hmac_key"):
            secret: SecretStr = getattr(self, name)
            value = secret.get_secret_value()
            if len(value) >= MIN_SECRET_LENGTH:
                continue
            if self.env in ("demo", "production"):
                raise ValueError(
                    f"{name} must be set to >= {MIN_SECRET_LENGTH} characters "
                    f"when AGENTCOSIGN_ENV={self.env}"
                )
            setattr(self, name, SecretStr(secrets.token_urlsafe(MIN_SECRET_LENGTH)))
            logger.warning("%s not set: using an ephemeral per-process secret", name)
        return self

    @property
    def is_hermetic(self) -> bool:
        """True when no live external dependency is used for any decision input."""
        return self.replay_mode and self.paypal_execution == "recorded"


def _find_config_path() -> Path:
    """Locate config.yaml: $AGENTCOSIGN_CONFIG, then CWD, then the source checkout.

    CWD is checked so a pip-installed package (e.g. in the Docker image, where the
    package lives in site-packages) still finds the config shipped beside the app.
    """
    explicit = os.environ.get("AGENTCOSIGN_CONFIG")
    if explicit:
        return Path(explicit)
    for candidate in (
        Path.cwd() / "config.yaml",
        Path(__file__).resolve().parent.parent.parent / "config.yaml",
    ):
        if candidate.is_file():
            return candidate
    return Path.cwd() / "config.yaml"


def _load_yaml_config(yaml_path: Path | None = None) -> dict[str, Any]:
    """Load config.yaml. A missing or malformed file aborts startup (fail closed)."""
    target_path = yaml_path or _find_config_path()
    if not target_path.is_file():
        raise ConfigurationError(f"config.yaml not found at {target_path}")
    try:
        with open(target_path, encoding="utf-8") as f:
            data = yaml.safe_load(f)
    except yaml.YAMLError as exc:
        raise ConfigurationError(f"config.yaml is not valid YAML: {exc}") from exc
    if not isinstance(data, dict):
        raise ConfigurationError("config.yaml must contain a mapping at top level")
    return data


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """Retrieve cached application settings instance.

    Raises:
        ConfigurationError: if config.yaml or the environment is invalid.
    """
    yaml_data = _load_yaml_config()
    sections = ("policy", "approval", "screening", "models", "audit")
    missing = [s for s in sections if s not in yaml_data]
    if missing:
        raise ConfigurationError(f"config.yaml is missing sections: {missing}")
    try:
        return Settings(**{s: yaml_data[s] for s in sections})
    except ValidationError as exc:
        raise ConfigurationError(f"invalid configuration: {exc}") from exc
