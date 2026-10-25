"""Operator commands for running AgentCoSign against the PayPal sandbox.

    python -m agentcosign.cli enrol-link
        Print a one-time link that sets up an approver passkey on a phone, so the
        operator key is never typed into the phone. It works for the time set in
        config.yaml (approval.enrolment_link_seconds).

    python -m agentcosign.cli propose --order ORDER_ID --amount 10.00
        Submit a refund proposal as the agent, the way an AI assistant would.

    python -m agentcosign.cli message --text "Order ... never arrived, refund $9"
        A customer writes in: the message is masked and kept, the AI assistant
        reads it and proposes a refund, and the gateway checks both.

Both need the gateway secrets set in the environment, because the running server
and this command must share them.
"""

import argparse
import asyncio
import json
import secrets
import sys
from collections.abc import Sequence
from datetime import UTC, datetime

import httpx

from agentcosign.clients.llm import OpenAICompatibleProvider
from agentcosign.clients.paypal import PayPalClient
from agentcosign.core.config import Settings, get_settings
from agentcosign.core.exceptions import AuditIntegrityError
from agentcosign.services.assistant import Suggestion, suggest
from agentcosign.services.gateway import (
    issue_token,
)
from agentcosign.services.gateway import (
    revoke_approver as revoke_passkeys,
)
from agentcosign.wiring import build_gateway, open_ledger, open_messages, open_passkeys

TIMEOUT_SECONDS = 30


def _out(text: str) -> None:
    sys.stdout.write(text + "\n")


def _require_shared_secrets(settings: Settings, names: Sequence[str]) -> bool:
    missing = [n for n in names if n in settings.ephemeral_secrets]
    if not missing:
        return True
    _out(
        "These secrets are not set in .env, so this command and the server would "
        "not share them: " + ", ".join(missing)
    )
    _out("Generate each with:")
    _out('  python -c "import secrets; print(secrets.token_urlsafe(48))"')
    return False


def enrol_link(settings: Settings) -> int:
    """Print a one-time approver enrolment link."""
    if not _require_shared_secrets(settings, ["audit_hmac_key"]):
        return 1
    secret = settings.audit_hmac_key.get_secret_value().encode("utf-8")
    token = issue_token(
        secret,
        "enrolment",
        secrets.token_hex(8),
        settings.approval.enrolment_link_seconds,
        datetime.now(UTC),
    )
    _out(f"{settings.public_origin}/approve#enrol={token}")
    minutes = settings.approval.enrolment_link_seconds // 60
    _out(f"Open it on the approver's phone within {minutes} minutes.")
    return 0


def propose(settings: Settings, order: str, amount: str, request_id: str | None) -> int:
    """Submit a refund proposal to the running gateway as the agent."""
    if not _require_shared_secrets(settings, ["agent_api_key"]):
        return 1
    body = {
        "request_id": request_id or f"cli-{secrets.token_hex(6)}",
        "tool": "create_refund",
        "amount": amount,
        "currency": "USD",
        "order_id": order,
    }
    return _post_proposal(settings, body)


def _post_proposal(settings: Settings, body: dict[str, str]) -> int:
    headers = {"Authorization": f"Bearer {settings.agent_api_key.get_secret_value()}"}
    response = httpx.post(
        f"{settings.public_origin}/v1/proposals",
        json=body,
        headers=headers,
        timeout=TIMEOUT_SECONDS,
    )
    _out(f"HTTP {response.status_code}")
    _out(json.dumps(response.json(), indent=2))
    return 0 if response.status_code == 200 else 1


def customer_message(settings: Settings, text: str) -> int:
    """A customer writes in: keep the masked message, let the AI assistant
    propose a refund for it, and send that proposal to the gateway."""
    if not _require_shared_secrets(settings, ["agent_api_key", "audit_hmac_key"]):
        return 1
    if not settings.groq_api_key.get_secret_value():
        _out("No model key is set, so the assistant cannot read the message.")
        return 1
    message_id = open_messages(settings).add(text, datetime.now(UTC))
    _out(f"Message kept as {message_id} (masked)")
    model = OpenAICompatibleProvider(
        "agent", settings.models.agent, settings.groq_api_key
    )
    suggestion = asyncio.run(_suggest(model, open_messages(settings).text(message_id)))
    if suggestion is None:
        _out("The assistant proposed no refund. A person should read this message.")
        return 0
    _out(
        f"Assistant proposes {suggestion.amount} {suggestion.currency} "
        f"on order {suggestion.order_id}"
    )
    return _post_proposal(
        settings,
        {
            "request_id": f"msg-{secrets.token_hex(6)}",
            "tool": "create_refund",
            "amount": f"{suggestion.amount:.2f}",
            "currency": suggestion.currency,
            "order_id": suggestion.order_id,
            "message_id": message_id,
        },
    )


async def _suggest(
    model: OpenAICompatibleProvider, text: str | None
) -> Suggestion | None:
    try:
        return None if text is None else await suggest(model, text)
    finally:
        await model.aclose()


def stop_refunds(settings: Settings) -> int:
    """Stop all refunds at once. Only the merchant can turn them back on, with
    their fingerprint in the app, so an operator can never restart money flow."""
    if not _require_shared_secrets(settings, ["audit_hmac_key"]):
        return 1
    ledger = open_ledger(settings)
    changed = ledger.set_refunds_stopped(True, "operator", datetime.now(UTC))
    _out("All refunds are stopped." if changed else "Refunds were already stopped.")
    still_sending = len(ledger.sending())
    if still_sending:
        _out(f"{still_sending} refund(s) already with PayPal will finish.")
    return 0


def revoke_approver(settings: Settings, name: str) -> int:
    """Revoke a lost or replaced phone: its passkey and open sessions stop working."""
    if not _require_shared_secrets(settings, ["audit_hmac_key"]):
        return 1
    count = revoke_passkeys(
        open_passkeys(settings), open_ledger(settings), name, datetime.now(UTC)
    )
    if not count:
        _out(f"No active passkey for {name}. Nothing changed.")
        return 1
    _out(f"Revoked {count} passkey(s) for {name}. Their sessions end now.")
    return 0


def audit_head(settings: Settings) -> int:
    """Print the chain head to publish somewhere this service cannot edit."""
    head = open_ledger(settings).head()
    _out(f"seq={head['seq']} hash={head['entry_hash']} at={head['created_at']}")
    return 0


def verify_head(settings: Settings, seq: int, entry_hash: str) -> int:
    """Check the chain still contains a head that was published earlier."""
    if not _require_shared_secrets(settings, ["audit_hmac_key"]):
        return 1
    try:
        count = open_ledger(settings).verify_against_head(seq, entry_hash)
    except AuditIntegrityError as exc:
        _out(f"FAILED: {exc.message}")
        return 1
    _out(f"OK: {count} entries verified; the published head at seq {seq} matches.")
    return 0


def reconcile(settings: Settings, paypal: PayPalClient | None = None) -> int:
    """Compare every refund sent in the look-back window with PayPal now."""
    if not _require_shared_secrets(settings, ["audit_hmac_key"]):
        return 1
    report = asyncio.run(build_gateway(settings, paypal).reconcile())
    counts = report.summary()
    _out(", ".join(f"{name} {value}" for name, value in counts.items()))
    for found in report.breaks:
        _out(f"break: {found.request_id} on order {found.order_id}: {found.kind}")
    for outside in report.outside:
        _out(f"outside the app: {outside.amount} on order {outside.order_id}")
    for order_id in report.unchecked:
        _out(f"could not check order {order_id}")
    return 0 if not report.breaks and not report.unchecked else 1


def main(argv: Sequence[str] | None = None) -> int:
    """Command-line entry point."""
    parser = argparse.ArgumentParser(prog="python -m agentcosign.cli")
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("enrol-link", help="one-time approver enrolment link")
    commands.add_parser("stop-refunds", help="stop all refunds now")
    commands.add_parser("audit-head", help="print the chain head to publish")
    commands.add_parser("reconcile", help="compare our refunds with PayPal now")
    check = commands.add_parser("verify-head", help="check against a published head")
    check.add_argument("--seq", type=int, required=True)
    check.add_argument("--hash", required=True)
    revoke = commands.add_parser("revoke-approver", help="revoke a lost phone")
    revoke.add_argument("--name", required=True)
    proposal = commands.add_parser(
        "propose", help="submit a refund proposal as the agent"
    )
    proposal.add_argument("--order", required=True)
    proposal.add_argument("--amount", required=True)
    proposal.add_argument("--request-id")
    message = commands.add_parser(
        "message", help="a customer message, read and proposed by the assistant"
    )
    message.add_argument("--text", required=True)
    args = parser.parse_args(argv)
    settings = get_settings()
    if args.command == "enrol-link":
        return enrol_link(settings)
    if args.command == "stop-refunds":
        return stop_refunds(settings)
    if args.command == "revoke-approver":
        return revoke_approver(settings, args.name)
    if args.command == "audit-head":
        return audit_head(settings)
    if args.command == "reconcile":
        return reconcile(settings)
    if args.command == "verify-head":
        return verify_head(settings, args.seq, args.hash)
    if args.command == "message":
        return customer_message(settings, args.text)
    return propose(settings, args.order, args.amount, args.request_id)


if __name__ == "__main__":
    sys.exit(main())
