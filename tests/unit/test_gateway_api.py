"""The gateway API end to end: propose, enrol, sign in, approve, execute, audit."""

import json
from collections.abc import Iterator
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from agentcosign.clients.paypal import RecordedPayPalClient
from agentcosign.core.config import Settings, get_settings
from agentcosign.main import create_app
from agentcosign.services.evidence import fetch_evidence
from tests.scenarios.policy_scenarios import NOW
from tests.unit.paypal_fixtures import ACCOUNT, order_json
from tests.unit.soft_authenticator import SoftAuthenticator

ORIGIN = "http://localhost:8000"


class Env:
    """A running app with a recorded PayPal and a software passkey."""

    def __init__(self, tmp_path: Path, merchant: str = ACCOUNT) -> None:
        self.settings: Settings = get_settings().model_copy(
            update={
                "sqlite_db_path": str(tmp_path / "ledger.db"),
                "paypal_merchant_id": merchant,
                "public_origin": ORIGIN,
            }
        )
        self.paypal = RecordedPayPalClient({"ORD-1": order_json(captured="500.00")})
        self.client = TestClient(create_app(self.settings, self.paypal))
        self.phone = SoftAuthenticator("localhost", ORIGIN)

    def key(self, name: str) -> dict[str, str]:
        secret = getattr(self.settings, name).get_secret_value()
        return {"Authorization": f"Bearer {secret}"}

    def propose(self, request_id: str, amount: str, **extra: Any) -> Any:
        body = {
            "request_id": request_id,
            "tool": "create_refund",
            "amount": amount,
            "currency": "USD",
            "order_id": "ORD-1",
            **extra,
        }
        return self.client.post(
            "/v1/proposals", json=body, headers=self.key("agent_api_key")
        )

    def enrol(self) -> None:
        operator = self.key("operator_api_key")
        options = self.client.post(
            "/v1/approver/enrolment/options",
            json={"approver": "Vikas"},
            headers=operator,
        ).text
        credential = self.phone.register(options)
        response = self.client.post(
            "/v1/approver/enrolment",
            json={"approver": "Vikas", "credential": credential},
            headers=operator,
        )
        assert response.status_code == 200

    def session(self) -> dict[str, str]:
        options = self.client.post("/v1/approver/session/options").text
        response = self.client.post(
            "/v1/approver/session", json={"credential": self.phone.sign(options)}
        )
        assert response.status_code == 200
        return {"X-Approver-Session": response.json()["session"]}

    def decide(self, request_id: str, intent: str, session: dict[str, str]) -> Any:
        options = self.client.post(
            f"/v1/approvals/{request_id}/options",
            json={"intent": intent},
            headers=session,
        ).text
        signed = self.phone.sign(options)
        return self.client.post(
            f"/v1/approvals/{request_id}/{intent}",
            json={"credential": signed},
            headers=session,
        ), signed

    async def refunded(self) -> Decimal:
        evidence = await fetch_evidence(self.paypal, "ORD-1", now=NOW, secret=b"x" * 32)
        return evidence.refunded_amount


@pytest.fixture
def env(tmp_path: Path) -> Iterator[Env]:
    environment = Env(tmp_path)
    with environment.client:
        yield environment


# --- the agent ------------------------------------------------------------------------
def test_a_proposal_is_escalated_while_screening_is_not_wired(env: Env) -> None:
    response = env.propose("r1", "10.00")
    assert response.status_code == 200
    body = response.json()
    assert body["decision"] == "escalate"
    assert body["rule_id"] == "R15"
    assert body["state"] == "pending_approval"


def test_repeating_a_proposal_replays_the_outcome(env: Env) -> None:
    env.propose("r1", "10.00")
    again = env.propose("r1", "10.00").json()
    assert again["replayed"] is True


def test_an_unreadable_order_is_denied(env: Env) -> None:
    body = env.propose("r1", "10.00", order_id="ORD-404").json()
    assert body["decision"] == "deny"
    assert body["rule_id"] == "R12"


@pytest.mark.parametrize(
    "change",
    [
        {"tool": "transfer_funds"},
        {"amount": "10.001"},
        {"amount": "-5"},
        {"currency": "usd"},
        {"request_id": "x" * 39},
        {"order_id": "../../v1"},
        {"surprise": "field"},
    ],
)
def test_malformed_proposals_are_refused_at_the_edge(
    env: Env, change: dict[str, str]
) -> None:
    body = {
        "request_id": "r1",
        "tool": "create_refund",
        "amount": "10.00",
        "currency": "USD",
        "order_id": "ORD-1",
        **change,
    }
    response = env.client.post(
        "/v1/proposals", json=body, headers=env.key("agent_api_key")
    )
    assert response.status_code == 422


def test_only_the_agent_key_can_propose(env: Env) -> None:
    body = {
        "request_id": "r1",
        "tool": "create_refund",
        "amount": "1.00",
        "currency": "USD",
    }
    assert env.client.post("/v1/proposals", json=body).status_code == 401
    operator = env.key("operator_api_key")
    assert (
        env.client.post("/v1/proposals", json=body, headers=operator).status_code == 401
    )


def test_status_is_readable_by_the_agent(env: Env) -> None:
    env.propose("r1", "10.00")
    agent = env.key("agent_api_key")
    assert (
        env.client.get("/v1/proposals/r1", headers=agent).json()["state"]
        == "pending_approval"
    )
    assert env.client.get("/v1/proposals/nope", headers=agent).status_code == 404


def test_without_a_merchant_id_every_order_is_refused(tmp_path: Path) -> None:
    environment = Env(tmp_path, merchant="")
    with environment.client:
        body = environment.propose("r1", "10.00").json()
    assert body["decision"] == "deny"


# --- enrolment and sessions ----------
def test_only_the_operator_can_enrol_an_approver(env: Env) -> None:
    body = {"approver": "Mallory"}
    path = "/v1/approver/enrolment/options"
    assert env.client.post(path, json=body).status_code == 401
    assert (
        env.client.post(path, json=body, headers=env.key("agent_api_key")).status_code
        == 401
    )


def test_listing_requires_a_passkey_session(env: Env) -> None:
    env.propose("r1", "10.00")
    assert env.client.get("/v1/approvals").status_code == 401
    assert (
        env.client.get(
            "/v1/approvals", headers={"X-Approver-Session": "a.1.b"}
        ).status_code
        == 401
    )
    env.enrol()
    listed = env.client.get("/v1/approvals", headers=env.session()).json()
    assert [item["request_id"] for item in listed] == ["r1"]
    assert listed[0]["amount"] == "10.00"


def test_the_agent_key_cannot_approve(env: Env) -> None:
    env.propose("r1", "10.00")
    response = env.client.post(
        "/v1/approvals/r1/approve",
        json={"credential": {}},
        headers=env.key("agent_api_key"),
    )
    assert response.status_code == 401


# --- approvals ----------
async def test_passkey_approval_executes_the_refund_once(env: Env) -> None:
    env.propose("r1", "10.00")
    env.enrol()
    session = env.session()
    response, signed = env.decide("r1", "approve", session)
    body = response.json()
    assert response.status_code == 200
    assert body["state"] == "executed"
    assert body["paypal_id"].startswith("RF-")
    assert body["paypal_status"] == "COMPLETED"
    assert "passkey" in body["explanation"]
    assert await env.refunded() == Decimal("10.00")
    replay = env.client.post(
        "/v1/approvals/r1/approve", json={"credential": signed}, headers=session
    )
    assert replay.status_code == 409
    assert await env.refunded() == Decimal("10.00")


async def test_passkey_rejection_moves_no_money(env: Env) -> None:
    env.propose("r1", "10.00")
    env.enrol()
    response, _ = env.decide("r1", "reject", env.session())
    assert response.json()["state"] == "denied"
    assert await env.refunded() == 0


def test_a_denied_request_cannot_be_approved(env: Env) -> None:
    env.propose("r1", "999.00")  # above the maximum: denied
    env.enrol()
    response = env.client.post(
        "/v1/approvals/r1/options", json={"intent": "approve"}, headers=env.session()
    )
    assert response.status_code == 409


def test_a_signature_for_one_request_cannot_approve_another(env: Env) -> None:
    env.propose("r1", "10.00")
    env.propose("r2", "20.00")
    env.enrol()
    session = env.session()
    options_r1 = env.client.post(
        "/v1/approvals/r1/options", json={"intent": "approve"}, headers=session
    ).text
    env.client.post(
        "/v1/approvals/r2/options", json={"intent": "approve"}, headers=session
    )
    response = env.client.post(
        "/v1/approvals/r2/approve",
        json={"credential": env.phone.sign(options_r1)},
        headers=session,
    )
    assert response.status_code == 409


def test_options_are_valid_webauthn_requests(env: Env) -> None:
    env.propose("r1", "10.00")
    env.enrol()
    options = json.loads(
        env.client.post(
            "/v1/approvals/r1/options",
            json={"intent": "approve"},
            headers=env.session(),
        ).text
    )
    assert options["rpId"] == "localhost"
    assert options["userVerification"] == "required"
    assert options["allowCredentials"][0]["id"] == env.phone.id


# --- one-time enrolment link and the approver page ----------
def test_a_one_time_link_enrols_without_the_operator_key(env: Env) -> None:
    from datetime import UTC, datetime

    from agentcosign.services.gateway import issue_token

    secret = env.settings.audit_hmac_key.get_secret_value().encode()
    link = issue_token(secret, "enrolment", "x", 600, datetime.now(UTC))
    headers = {"X-Enrolment-Token": link}
    options = env.client.post(
        "/v1/approver/enrolment/options", json={"approver": "Vikas"}, headers=headers
    ).text
    response = env.client.post(
        "/v1/approver/enrolment",
        json={"approver": "Vikas", "credential": env.phone.register(options)},
        headers=headers,
    )
    assert response.status_code == 200


@pytest.mark.parametrize("purpose", ["approver-session", "enrolment"])
def test_forged_or_expired_links_are_refused(env: Env, purpose: str) -> None:
    from datetime import UTC, datetime, timedelta

    from agentcosign.services.gateway import issue_token

    secret = env.settings.audit_hmac_key.get_secret_value().encode()
    expired = issue_token(
        secret, "enrolment", "x", 600, datetime.now(UTC) - timedelta(hours=1)
    )
    wrong_purpose = issue_token(secret, "approver-session", "x", 600, datetime.now(UTC))
    other_key = issue_token(b"k" * 32, "enrolment", "x", 600, datetime.now(UTC))
    for token in (expired, wrong_purpose, other_key, "garbage"):
        response = env.client.post(
            "/v1/approver/enrolment/options",
            json={"approver": "Mallory"},
            headers={"X-Enrolment-Token": token},
        )
        assert response.status_code == 401


def test_the_approver_page_is_served_with_the_strict_policy(env: Env) -> None:
    page = env.client.get("/approve")
    assert page.status_code == 200
    assert "Refund requests" in page.text
    assert "script-src 'self'" in page.headers["content-security-policy"]
    script = env.client.get("/assets/approve.js").text
    assert "innerHTML" not in script
    assert "textContent" in script


def test_the_approver_sees_owner_words_and_live_paypal_facts(env: Env) -> None:
    env.propose("r1", "10.00")
    env.enrol()
    item = env.client.get("/v1/approvals", headers=env.session()).json()[0]
    assert item["customer"] == "Jane Buyer"  # read live from PayPal, not stored
    assert item["paid_with"] == "card ending 4242"
    assert item["paid"] == "500.00"
    assert item["already_refunded"] == "0.00"
    assert item["safety_check"] == "unavailable"  # missing is never "passed"
    assert item["reasons"] == [
        "Your assistant could usually send this on its own. It needs your OK "
        "because our safety check did not run."
    ]
    assert item["left_after"] == "490.00"
    assert item["refundable_now"] == "500.00"
    assert item["fits"] == "yes"
    assert "R15" not in json.dumps(item)
    assert item["decide_by"] > item["asked_at"]


async def test_a_refund_made_in_paypal_while_waiting_cannot_overspend(env: Env) -> None:
    # 500.00 was paid; the assistant asks for 200.00, then 400.00 is refunded in
    # PayPal directly while the request waits, leaving only 100.00
    env.propose("r1", "200.00")
    await env.paypal.refund_capture("CAP-1", Decimal("400.00"), "USD", "outside-1")
    env.enrol()
    session = env.session()
    first, _ = env.decide("r1", "approve", session)
    # the order changed, so the approver is asked again with today's figures
    assert first.json()["state"] == "pending_approval"
    assert await env.refunded() == Decimal("400.00")
    second, _ = env.decide("r1", "approve", session)
    # approving again cannot return more than was paid
    assert second.json()["state"] == "failed"
    assert await env.refunded() == Decimal("400.00")
