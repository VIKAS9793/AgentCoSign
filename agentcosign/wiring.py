"""Builds the services the same way for the web app and the command line.

Nothing here builds the web app, so an operator command such as stopping all
refunds keeps working when the web app itself cannot load.
"""

import hashlib
import hmac
from pathlib import Path

from pydantic import SecretStr

from agentcosign.clients.llm import OpenAICompatibleProvider, QuotaListener
from agentcosign.clients.paypal import (
    LivePayPalClient,
    PayPalClient,
    RecordedPayPalClient,
)
from agentcosign.core.config import Settings
from agentcosign.services.ai_checks import CustomerChecks
from agentcosign.services.gateway import Gateway
from agentcosign.services.ledger import Ledger
from agentcosign.services.messages import MessageStore
from agentcosign.services.passkeys import PasskeyRegistry
from agentcosign.services.workspaces import Workspaces

RECORDINGS_DIR = (
    Path(__file__).resolve().parent.parent / "docs" / "evidence" / "recordings"
)


def paypal_client(settings: Settings) -> PayPalClient:
    """Live sandbox calls when configured, otherwise recorded sandbox responses."""
    if settings.paypal_execution == "sandbox":
        return LivePayPalClient(
            settings.paypal_client_id, settings.paypal_client_secret
        )
    return RecordedPayPalClient.from_directory(
        RECORDINGS_DIR, settings.paypal_merchant_id
    )


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


def open_messages(settings: Settings) -> MessageStore:
    """Masked customer messages, kept in the same database as the ledger."""
    return MessageStore(
        _database(settings),
        _audit_secret(settings),
        settings.screening.max_document_chars,
    )


def build_checks(
    settings: Settings, on_quota: QuotaListener | None = None
) -> CustomerChecks | None:
    """The AI checks on customer messages, or None when no model key is set (then
    every request with a message is treated as unchecked and asks the merchant)."""
    key = settings.groq_api_key
    if not key.get_secret_value():
        return None
    models = settings.models
    return CustomerChecks(
        guard_model=OpenAICompatibleProvider(
            "guard", models.guard, key, on_quota=on_quota
        ),
        screening_model=OpenAICompatibleProvider(
            "screening", models.screening, key, on_quota=on_quota
        ),
        reading_model=OpenAICompatibleProvider(
            "extraction", models.extraction, key, on_quota=on_quota
        ),
        threshold=settings.screening.guard_threshold,
    )


def build_assistant(
    settings: Settings, on_quota: QuotaListener | None = None
) -> OpenAICompatibleProvider | None:
    """The refund assistant's model, or None when no model key is set."""
    key = settings.groq_api_key
    if not key.get_secret_value():
        return None
    return OpenAICompatibleProvider(
        "agent", settings.models.agent, key, on_quota=on_quota
    )


def build_gateway(
    settings: Settings,
    paypal: PayPalClient | None = None,
    on_quota: QuotaListener | None = None,
) -> Gateway:
    """The gateway as the service runs it; the command line uses the same one."""
    return Gateway(
        ledger=open_ledger(settings),
        passkeys=open_passkeys(settings),
        paypal=paypal or paypal_client(settings),
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
        messages=open_messages(settings),
        checks=build_checks(settings, on_quota),
    )


def build_workspaces(
    settings: Settings,
    default: Gateway,
    paypal: PayPalClient | None = None,
    on_quota: QuotaListener | None = None,
) -> Workspaces[Gateway]:
    """The owner's gateway plus, in the hosted demo, one private gateway per visitor.

    Each visitor's workspace has its own database and its own signing secret,
    derived from the audit key and the workspace id, so a sign-in or setup link
    from one workspace is never accepted by another.
    """
    shared_paypal = paypal or paypal_client(settings)
    folder = _database(settings).parent / "workspaces"
    master = settings.audit_hmac_key.get_secret_value().encode("utf-8")

    def build(workspace_id: str) -> Gateway:
        """One workspace's gateway: its own database file and signing secret."""
        folder.mkdir(parents=True, exist_ok=True)
        secret = hmac.new(master, workspace_id.encode("utf-8"), hashlib.sha256)
        own = settings.model_copy(
            update={
                "sqlite_db_path": str(folder / f"{workspace_id}.db"),
                "audit_hmac_key": SecretStr(secret.hexdigest()),
            }
        )
        return build_gateway(own, shared_paypal, on_quota)

    return Workspaces(
        default=default, build=build, demo=settings.env == "demo", limits=settings.demo
    )
