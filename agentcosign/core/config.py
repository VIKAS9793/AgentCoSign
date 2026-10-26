"""Configuration loader for AgentCoSign.

Combines config.yaml (Single Source of Truth) with environment variables.

Design rules:
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
from urllib.parse import urlparse

import yaml
from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    PrivateAttr,
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
    refund_auto_limit: Decimal  # at or below: allowed without a human
    refund_max_limit: Decimal  # above: denied; between the two: escalated
    velocity_max_refunds_24h: int = Field(ge=1, le=100)  # per customer, rolling 24h
    payout_new_payee_action: Literal["escalate", "deny"]  # never "allow"
    payout_duplicate_window_days: int = Field(ge=1, le=365)  # repeat-payout window
    daily_outflow_cap: Decimal  # executed plus reserved, rolling 24h
    # Evidence older than this is not trusted for a decision; it is re-read first.
    evidence_max_age_seconds: int = Field(ge=1, le=3_600)

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

    # How long a request waits for the owner. Every approval re-checks PayPal and
    # every rule before anything is sent, so a long wait cannot act on old facts;
    # the cap still keeps money from being set aside for more than a day.
    open_seconds: int = Field(ge=300, le=86_400)
    # How long one fingerprint prompt can be answered: short, so a prompt left open
    # on an unattended phone cannot be completed much later.
    fingerprint_seconds: int = Field(ge=30, le=300)
    # How long a signed-in phone stays open, and how long a setup link works.
    # Both are capped at 10 minutes in code for the same reason.
    session_seconds: int = Field(ge=60, le=600)
    enrolment_link_seconds: int = Field(ge=60, le=600)


class ScreeningSettings(BaseModel):
    """Prompt-injection screening behaviour."""

    model_config = _STRICT

    on_unavailable: Literal["escalate", "deny"]  # never "allow"
    max_document_chars: int = Field(ge=500, le=50_000)
    # The prompt guard's score at or above which a message counts as an attempt
    # to instruct the assistant. A probability: 0 would flag everything, 1 nothing.
    guard_threshold: Decimal

    @field_validator("guard_threshold", mode="before")
    @classmethod
    def _parse_threshold(cls, value: Any) -> Decimal:
        threshold = _to_decimal(value)
        if not Decimal(0) < threshold < Decimal(1):
            raise ValueError("guard_threshold must be between 0 and 1")
        return threshold


class ModelRoleConfig(BaseModel):
    """Configuration for an individual model role."""

    model_config = _STRICT

    provider: Literal["groq", "nvidia_nim"]
    primary: str = Field(min_length=1)
    fallback: str = Field(min_length=1)
    # guardrail: allow float-annotation -- sampling temperature, not money
    temperature: float = Field(ge=0.0, le=2.0)
    max_tokens: int = Field(ge=16, le=16_384)
    timeout_seconds: int = Field(ge=1, le=120)
    base_url: str | None = None


# Providers whose terms let the account switch off keeping prompts and rule out
# training on them. The free NVIDIA trial may use prompts to improve its models,
# so it never receives a customer's words.
CUSTOMER_TEXT_PROVIDERS = frozenset({"groq"})


class ModelsConfig(BaseModel):
    """Configuration for all model tiers."""

    model_config = _STRICT

    agent: ModelRoleConfig
    screening: ModelRoleConfig
    extraction: ModelRoleConfig
    guard: ModelRoleConfig

    @model_validator(mode="after")
    def _customer_text_stays_with_cleared_providers(self) -> "ModelsConfig":
        # every role reads the customer's message, the agent included
        roles = {"agent": self.agent, "screening": self.screening}
        roles.update(extraction=self.extraction, guard=self.guard)
        for name, role in roles.items():
            if role.provider not in CUSTOMER_TEXT_PROVIDERS:
                raise ValueError(
                    f"{name} reads customer messages; provider {role.provider} is "
                    "not cleared for customer text"
                )
        return self

    @model_validator(mode="after")
    def _second_reading_is_independent(self) -> "ModelsConfig":
        # the extraction is compared with the agent's proposal, so it must not
        # be the same model agreeing with itself
        if self.extraction.primary == self.agent.primary:
            raise ValueError("extraction must be independent of the agent's model")
        return self


class BucketSpec(BaseModel):
    """A token bucket: holds ``capacity`` requests and gains one per interval."""

    model_config = _STRICT

    capacity: int = Field(ge=1, le=10_000)
    refill_seconds: int = Field(ge=1, le=86_400)


class TierLimits(BaseModel):
    """Per-client buckets for one audience, by kind of work."""

    model_config = _STRICT

    general: BucketSpec  # every request
    live_llm: BucketSpec  # asks for a live model call
    live_execution: BucketSpec  # asks for a live sandbox execution


class DailyPools(BaseModel):
    """How many live calls per 24 hours each audience may use."""

    model_config = _STRICT

    public: int = Field(ge=0, le=100_000)
    reviewer: int = Field(ge=0, le=100_000)


class LlmBudget(DailyPools):
    """Live model calls, bounded by what the provider's free tier allows."""

    provider_daily_limit: int = Field(ge=1, le=1_000_000)

    @model_validator(mode="after")
    def _pools_fit_provider_limit(self) -> "LlmBudget":
        if self.public + self.reviewer > self.provider_daily_limit:
            raise ValueError("public + reviewer pools exceed the provider daily limit")
        return self


class PenaltySettings(BaseModel):
    """Escalating cool-down for clients that keep hitting the limits."""

    model_config = _STRICT

    strikes: int = Field(ge=1, le=1_000)
    strike_window_seconds: int = Field(ge=1, le=3_600)
    block_seconds: int = Field(ge=1, le=3_600)
    max_block_seconds: int = Field(ge=1, le=86_400)

    @model_validator(mode="after")
    def _block_ordering(self) -> "PenaltySettings":
        if self.max_block_seconds < self.block_seconds:
            raise ValueError("max_block_seconds must be >= block_seconds")
        return self


class LimitSettings(BaseModel):
    """Traffic protection: the hard floor that keeps the service alive.

    Per-client buckets are fairness. The global caps (in-flight requests, daily
    pools, body size) are what hold when client identity cannot be trusted.
    """

    model_config = _STRICT

    trusted_proxy_hops: int = Field(ge=0, le=5)
    # Header the edge sets to the connecting address ("" to disable). Preferred over
    # the forwarding chain because it does not depend on the hop count.
    client_ip_header: str = Field(pattern=r"^[a-z0-9-]*$", max_length=64)
    # Calibration aid: shows the forwarding chain as received. Turn off when done.
    expose_client_diagnostics: bool
    max_body_bytes: int = Field(ge=1_024, le=1_048_576)
    max_concurrent_requests: int = Field(ge=1, le=256)
    # In-flight slots only reviewers may use, so a public flood cannot lock them out.
    reviewer_reserved_concurrency: int = Field(ge=0, le=255)
    max_tracked_clients: int = Field(ge=100, le=100_000)
    exempt_paths: list[str]
    # Live update streams stay open for minutes, so they are counted against
    # their own cap instead of the request slots everyone else needs.
    streaming_paths: list[str]
    max_open_streams: int = Field(ge=1, le=256)
    public: TierLimits
    reviewer: TierLimits
    live_llm_budget: LlmBudget
    live_execution_budget: DailyPools
    provider_low_percent: int = Field(ge=0, le=50)
    penalty: PenaltySettings

    @field_validator("exempt_paths", "streaming_paths")
    @classmethod
    def _paths_are_absolute(cls, value: list[str]) -> list[str]:
        if any(not p.startswith("/") for p in value):
            raise ValueError("paths must start with '/'")
        return value

    @model_validator(mode="after")
    def _reserve_leaves_room_for_the_public(self) -> "LimitSettings":
        if self.reviewer_reserved_concurrency >= self.max_concurrent_requests:
            raise ValueError(
                "reviewer_reserved_concurrency must be below max_concurrent_requests"
            )
        return self

    @model_validator(mode="after")
    def _reviewers_are_never_stricter(self) -> "LimitSettings":
        for name in ("general", "live_llm", "live_execution"):
            public: BucketSpec = getattr(self.public, name)
            reviewer: BucketSpec = getattr(self.reviewer, name)
            if reviewer.capacity < public.capacity:
                raise ValueError(f"reviewer {name} capacity is below public")
            if reviewer.refill_seconds > public.refill_seconds:
                raise ValueError(f"reviewer {name} refills slower than public")
        return self


class ReconciliationSettings(BaseModel):
    """How the daily comparison with PayPal runs."""

    model_config = _STRICT

    lookback_days: int = Field(ge=1, le=31)
    stale_processing_hours: int = Field(ge=1, le=168)
    interval_hours: int = Field(ge=1, le=168)


class AuditSettings(BaseModel):
    """How often the whole audit record is checked in the background."""

    model_config = _STRICT

    full_check_hours: int = Field(ge=1, le=168)


class DisplaySettings(BaseModel):
    """How the merchant's screens read PayPal (display only, never decisions)."""

    model_config = _STRICT

    max_parallel_reads: int = Field(ge=1, le=10)
    # kept short: a screen must never show old facts for long
    reuse_seconds: int = Field(ge=0, le=60)


class DemoSettings(BaseModel):
    """Limits for the hosted demo, where each visitor gets a private workspace."""

    model_config = _STRICT

    max_workspaces: int = Field(ge=1, le=2_000)
    setups_per_address_per_day: int = Field(ge=1, le=100)
    scenarios_per_workspace_per_day: int = Field(ge=1, le=500)
    message_max_chars: int = Field(ge=50, le=2_000)


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

    # Degradation switches: how far a run depends on live services.
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
    # The merchant account orders must belong to; empty means every order is
    # refused as belonging to an unknown account.
    paypal_merchant_id: str = Field(default="", alias="PAYPAL_MERCHANT_ID")

    # The address approvers use; passkeys are bound to its host name.
    public_origin: str = Field(
        default="http://localhost:8000", alias="AGENTCOSIGN_PUBLIC_ORIGIN"
    )

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

    # Optional shared token that raises live-call quotas for reviewers. Without it
    # everyone uses the public quotas; nothing is gated behind it.
    reviewer_token: SecretStr = Field(
        default=SecretStr(""), alias="AGENTCOSIGN_REVIEWER_TOKEN"
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
    reconciliation: ReconciliationSettings
    audit: AuditSettings
    display: DisplaySettings
    demo: DemoSettings
    limits: LimitSettings

    _ephemeral: set[str] = PrivateAttr(default_factory=set)

    @model_validator(mode="after")
    def _reviewer_token_is_strong_when_set(self) -> "Settings":
        token = self.reviewer_token.get_secret_value()
        if token and len(token) < MIN_SECRET_LENGTH:
            raise ValueError(
                f"reviewer_token must be >= {MIN_SECRET_LENGTH} characters when set"
            )
        return self

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
            self._ephemeral.add(name)
            logger.warning("%s not set: using an ephemeral per-process secret", name)
        return self

    @field_validator("public_origin")
    @classmethod
    def _origin_is_secure(cls, value: str) -> str:
        """Passkeys work only over HTTPS, or plain HTTP on the local machine."""
        parsed = urlparse(value)
        local = parsed.hostname in ("localhost", "127.0.0.1")
        secure = parsed.scheme == "https" or (parsed.scheme == "http" and local)
        if secure and parsed.path in ("", "/") and parsed.hostname:
            return value.rstrip("/")
        raise ValueError("public_origin must be an https origin or http://localhost")

    @property
    def ephemeral_secrets(self) -> tuple[str, ...]:
        """Gateway secrets generated for this process only (not set in the env)."""
        return tuple(sorted(self._ephemeral))

    @property
    def passkey_rp_id(self) -> str:
        """The host name passkeys are bound to."""
        return urlparse(self.public_origin).hostname or "localhost"

    @property
    def has_live_models(self) -> bool:
        """True when the AI models are called live (a model key is set)."""
        return bool(self.groq_api_key.get_secret_value())

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
    sections = (
        "policy",
        "approval",
        "screening",
        "models",
        "limits",
        "reconciliation",
        "audit",
        "display",
        "demo",
    )
    missing = [s for s in sections if s not in yaml_data]
    if missing:
        raise ConfigurationError(f"config.yaml is missing sections: {missing}")
    try:
        return Settings(**{s: yaml_data[s] for s in sections})
    except ValidationError as exc:
        raise ConfigurationError(f"invalid configuration: {exc}") from exc
