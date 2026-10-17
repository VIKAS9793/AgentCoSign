"""Builds the services the same way for the web app and the command line.

Nothing here builds the web app, so an operator command such as stopping all
refunds keeps working when the web app itself cannot load.
"""

from pathlib import Path

from agentcosign.clients.paypal import (
    LivePayPalClient,
    PayPalClient,
    RecordedPayPalClient,
)
from agentcosign.core.config import Settings
from agentcosign.services.gateway import Gateway
from agentcosign.services.ledger import Ledger
from agentcosign.services.passkeys import PasskeyRegistry

RECORDINGS_DIR = (
    Path(__file__).resolve().parent.parent / "docs" / "evidence" / "recordings"
)


def _paypal_client(settings: Settings) -> PayPalClient:
    """Live sandbox calls when configured, otherwise recorded sandbox responses."""
    if settings.paypal_execution == "sandbox":
        return LivePayPalClient(
            settings.paypal_client_id, settings.paypal_client_secret
        )
    return RecordedPayPalClient.from_directory(RECORDINGS_DIR)


def _audit_secret(settings: Settings) -> bytes:
    return settings.audit_hmac_key.get_secret_value().encode("utf-8")


def _database(settings: Settings) -> Path:
    """The shared database file, with its folder created whichever store opens first."""
    database = Path(settings.sqlite_db_path)
    database.parent.mkdir(parents=True, exist_ok=True)
    return database


def open_ledger(settings: Settings) -> Ledger:
    """The service's ledger, opened the same way by the app and the command line."""
    return Ledger(
        _database(settings),
        _audit_secret(settings),
        settings.policy,
        settings.paypal_merchant_id,
    )


def open_passkeys(settings: Settings) -> PasskeyRegistry:
    """The approver passkeys, kept in the same database as the ledger."""
    return PasskeyRegistry(
        _database(settings),
        settings.passkey_rp_id,
        settings.public_origin,
        prompt_seconds=settings.approval.fingerprint_seconds,
    )


def build_gateway(settings: Settings, paypal: PayPalClient | None = None) -> Gateway:
    """The gateway as the service runs it; the command line uses the same one."""
    return Gateway(
        ledger=open_ledger(settings),
        passkeys=open_passkeys(settings),
        paypal=paypal or _paypal_client(settings),
        policy=settings.policy,
        screening=settings.screening,
        secret=_audit_secret(settings),
        approval=settings.approval,
        reconciliation=settings.reconciliation,
        audit=settings.audit,
        display=settings.display,
        # only live PayPal can verify a webhook; recorded mode refuses them all
        webhook_id=(
            settings.paypal_webhook_id if settings.paypal_execution == "sandbox" else ""
        ),
    )
