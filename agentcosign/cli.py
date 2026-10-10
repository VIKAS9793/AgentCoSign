"""Operator commands for running AgentCoSign against the PayPal sandbox.

    python -m agentcosign.cli enrol-link
        Print a one-time link (valid 10 minutes) that sets up an approver passkey
        on a phone, so the operator key is never typed into the phone.

    python -m agentcosign.cli propose --order ORDER_ID --amount 10.00
        Submit a refund proposal as the agent, the way an AI assistant would.

Both need the gateway secrets set in the environment, because the running server
and this command must share them.
"""

import argparse
import json
import secrets
import sys
from collections.abc import Sequence
from datetime import UTC, datetime

import httpx

from agentcosign.core.config import Settings, get_settings
from agentcosign.services.gateway import ENROLMENT_LINK_SECONDS, issue_token

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
        ENROLMENT_LINK_SECONDS,
        datetime.now(UTC),
    )
    _out(f"{settings.public_origin}/approve#enrol={token}")
    _out("Open it on the approver's phone within 10 minutes.")
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


def main(argv: Sequence[str] | None = None) -> int:
    """Command-line entry point."""
    parser = argparse.ArgumentParser(prog="python -m agentcosign.cli")
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("enrol-link", help="one-time approver enrolment link")
    proposal = commands.add_parser(
        "propose", help="submit a refund proposal as the agent"
    )
    proposal.add_argument("--order", required=True)
    proposal.add_argument("--amount", required=True)
    proposal.add_argument("--request-id")
    args = parser.parse_args(argv)
    settings = get_settings()
    if args.command == "enrol-link":
        return enrol_link(settings)
    return propose(settings, args.order, args.amount, args.request_id)


if __name__ == "__main__":
    sys.exit(main())
