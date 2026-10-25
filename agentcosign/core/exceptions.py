"""Exception hierarchy for AgentCoSign.

All exceptions inherit from AgentCoSignError.
Under the fail-closed pattern, critical exceptions result in DENY.
"""

from typing import Any


class AgentCoSignError(Exception):
    """Base exception for all AgentCoSign runtime errors."""

    def __init__(self, message: str, details: dict[str, Any] | None = None) -> None:
        super().__init__(message)
        self.message = message
        self.details = details or {}


class ConfigurationError(AgentCoSignError):
    """Raised at startup when configuration is missing or unsafe.

    Never caught and downgraded: a gateway with bad config must not start.
    """


class FailClosedError(AgentCoSignError):
    """Raised when an unrecoverable error forces a fail-closed DENY outcome."""


class SignatureError(FailClosedError):
    """Raised when a passkey signature cannot be accepted.

    The passkey is unknown, the signature does not verify, or the request to
    sign expired or was already used. Nothing was decided, so the person can
    try again; this is told apart from a request that cannot be decided.
    """


class PolicyEvaluationError(AgentCoSignError):
    """Raised when a policy evaluation fails or cannot resolve rules."""


class ScreeningError(AgentCoSignError):
    """Raised when prompt-injection or anomaly screening encounters a failure."""


class ExtractionError(AgentCoSignError):
    """Raised when structured financial extraction fails."""


class ModelUnavailableError(AgentCoSignError):
    """Raised when no configured model gave a usable answer in time.

    The check counts as "could not run", which asks the owner and never allows.
    """


class AuditIntegrityError(AgentCoSignError):
    """Raised when hash-chain validation or audit ledger integrity fails."""


class PayPalClientError(AgentCoSignError):
    """Raised when communication with PayPal sandbox fails."""
