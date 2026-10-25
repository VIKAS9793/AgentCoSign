"""The gateway API end to end: propose, enrol, sign in, approve, execute, audit."""

import asyncio
import json
import re
import sqlite3
from collections.abc import Iterator
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from agentcosign.clients.paypal import RecordedPayPalClient
from agentcosign.core.config import Settings, get_settings
from agentcosign.core.exceptions import (
    AuditIntegrityError,
    FailClosedError,
    PayPalClientError,
)
from agentcosign.main import create_app
from agentcosign.models.policy import ScreeningVerdict, Signals
from agentcosign.services.evidence import fetch_evidence
from agentcosign.services.gateway import RETRY_QUIET_SECONDS
from tests.scenarios.policy_scenarios import NOW
from tests.unit.paypal_fixtures import ACCOUNT, dispute_json, order_json
from tests.unit.soft_authenticator import SoftAuthenticator
from tests.unit.web_js import run_js as _run_js
from tests.unit.web_js import script_function as _script_function

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

    def enrol(self, name: str = "Vikas") -> None:
        operator = self.key("operator_api_key")
        options = self.client.post(
            "/v1/approver/enrolment/options",
            json={"approver": name},
            headers=operator,
        ).text
        credential = self.phone.register(options)
        response = self.client.post(
            "/v1/approver/enrolment",
            json={"approver": name, "credential": credential},
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

    def seen(self, order: str = "ORD-1") -> dict[str, str] | None:
        """The figures a phone shows for the order, read from PayPal now."""
        read = fetch_evidence(self.paypal, order, now=NOW, secret=b"x" * 32)
        try:
            # its own thread, so async tests (already in a loop) can call it too
            with ThreadPoolExecutor(max_workers=1) as pool:
                facts = pool.submit(asyncio.run, read).result()
        except PayPalClientError:
            return None
        return {
            "paid": f"{facts.captured_amount:.2f}",
            "already_refunded": f"{facts.refunded_amount:.2f}",
        }

    def decide(
        self,
        request_id: str,
        intent: str,
        session: dict[str, str],
        seen: dict[str, str] | None = None,
    ) -> Any:
        body: dict[str, Any] = {"intent": intent}
        shown = seen if seen is not None else self.seen()
        if intent == "approve" and shown is not None:
            body["seen"] = shown
        response = self.client.post(
            f"/v1/approvals/{request_id}/options", json=body, headers=session
        )
        if response.status_code != 200:
            return response, None
        signed = self.phone.sign(response.text)
        return self.client.post(
            f"/v1/approvals/{request_id}/{intent}",
            json={"credential": signed},
            headers=session,
        ), signed

    async def refunded(self) -> Decimal:
        evidence = await fetch_evidence(self.paypal, "ORD-1", now=NOW, secret=b"x" * 32)
        return evidence.refunded_amount


def _listed(env: Env, session: dict[str, str]) -> list[dict[str, Any]]:
    """Every open request as the merchant's home shows it, in all sections."""
    home = env.client.get("/v1/approvals/home", headers=session).json()
    return [item for items in home["sections"].values() for item in items]


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
    assert env.client.get("/v1/approvals/home").status_code == 401
    assert (
        env.client.get(
            "/v1/approvals/home", headers={"X-Approver-Session": "a.1.b"}
        ).status_code
        == 401
    )
    env.enrol()
    listed = _listed(env, env.session())
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


@pytest.mark.edge
def test_a_denied_request_cannot_be_approved(env: Env) -> None:
    env.propose("r1", "999.00")  # above the maximum: denied
    env.enrol()
    response = env.client.post(
        "/v1/approvals/r1/options",
        json={"intent": "approve", "seen": env.seen()},
        headers=env.session(),
    )
    assert response.status_code == 409


@pytest.mark.edge
def test_a_signature_for_one_request_cannot_approve_another(env: Env) -> None:
    env.propose("r1", "10.00")
    env.propose("r2", "20.00")
    env.enrol()
    session = env.session()
    options_r1 = env.client.post(
        "/v1/approvals/r1/options",
        json={"intent": "approve", "seen": env.seen()},
        headers=session,
    ).text
    env.client.post(
        "/v1/approvals/r2/options",
        json={"intent": "approve", "seen": env.seen()},
        headers=session,
    )
    response = env.client.post(
        "/v1/approvals/r2/approve",
        json={"credential": env.phone.sign(options_r1)},
        headers=session,
    )
    # signed for the first request, refused for the other
    assert response.status_code == 403


def test_options_are_valid_webauthn_requests(env: Env) -> None:
    env.propose("r1", "10.00")
    env.enrol()
    options = json.loads(
        env.client.post(
            "/v1/approvals/r1/options",
            json={"intent": "approve", "seen": env.seen()},
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
@pytest.mark.edge
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
    assert "Refund requests from your AI assistant" in page.text
    assert "script-src 'self'" in page.headers["content-security-policy"]
    script = env.client.get("/assets/approve.js").text
    assert "innerHTML" not in script
    assert "textContent" in script


def test_the_approver_sees_owner_words_and_live_paypal_facts(env: Env) -> None:
    env.propose("r1", "10.00")
    env.enrol()
    item = _listed(env, env.session())[0]
    assert item["customer"] == "Jane Buyer"  # read live from PayPal, not stored
    assert item["paid_with"] == "card ending 4242"
    assert item["paid"] == "500.00"
    assert item["already_refunded"] == "0.00"
    assert item["safety_check"] == "unavailable"  # missing is never "passed"
    assert item["reasons"] == [
        "This would normally be refunded on its own, but we couldn't scan the "
        "customer's messages for misuse."
    ]
    assert item["left_after"] == "490.00"
    assert item["refundable_now"] == "500.00"
    assert item["fits"] == "yes"
    assert "R15" not in json.dumps(item)
    assert item["decide_by"] > item["asked_at"]


@pytest.mark.edge
async def test_a_refund_made_in_paypal_while_waiting_cannot_overspend(env: Env) -> None:
    # 500.00 was paid; the assistant asks for 200.00, then 400.00 is refunded in
    # PayPal directly while the request waits, leaving only 100.00
    env.propose("r1", "200.00")
    await env.paypal.refund_capture("CAP-1", Decimal("400.00"), "USD", "outside-1")
    env.enrol()
    session = env.session()
    first, _ = env.decide("r1", "approve", session)
    # the approval checks PayPal again first: 200.00 does not fit now, so nothing
    # is sent and the owner is told why in their words
    assert first.json()["state"] == "denied"
    assert "isn't enough left" in first.json()["merchant_reason"]
    assert await env.refunded() == Decimal("400.00")


def _decide_by(env: Env, session: dict[str, str]) -> str:
    return str(
        next(i for i in _listed(env, session) if i["request_id"] == "r1")["decide_by"]
    )


@pytest.mark.edge
async def test_a_refund_elsewhere_already_on_screen_is_approved_without_asking_again(
    env: Env,
) -> None:
    # 500.00 paid; 200.00 asked; 100.00 refunded in PayPal directly while it
    # waits, and the owner's screen shows the 100.00 when they approve
    env.propose("r1", "200.00")
    env.enrol()
    session = env.session()
    await env.paypal.refund_capture("CAP-1", Decimal("100.00"), "USD", "outside-1")
    outcome, _ = env.decide("r1", "approve", session)
    assert outcome.json()["state"] == "executed"
    assert await env.refunded() == Decimal("300.00")


@pytest.mark.edge
async def test_a_dispute_opened_while_waiting_is_shown_before_anything_is_sent(
    env: Env,
) -> None:
    env.propose("r1", "200.00")
    env.enrol()
    session = env.session()
    env.paypal._disputes["CAP-1"] = [dispute_json(amount="200.00")]  # type: ignore[attr-defined]  # a customer opens a dispute in PayPal
    first, _ = env.decide("r1", "approve", session)
    assert first.json()["state"] == "pending_approval"  # asked again, nothing sent
    assert await env.refunded() == Decimal("0")
    card = next(i for i in _listed(env, session) if i["request_id"] == "r1")
    # the owner is told about the dispute before they can approve again
    assert any("dispute" in reason for reason in card["reasons"])
    assert any("over $25.00" in reason for reason in card["reasons"])  # kept
    assert card["flag"] == "Open dispute"
    record = env.client.get("/v1/approvals/r1/history", headers=session).json()
    assert record["sealed"] is True


@pytest.mark.edge
async def test_figures_that_changed_after_the_screen_read_get_no_fingerprint(
    env: Env,
) -> None:
    env.propose("r1", "200.00")
    env.enrol()
    session = env.session()
    on_screen = env.seen()  # read before the refund made elsewhere
    await env.paypal.refund_capture("CAP-1", Decimal("100.00"), "USD", "outside-1")
    refused, _ = env.decide("r1", "approve", session, seen=on_screen)
    assert refused.status_code == 409
    assert _gateway(env)._ledger.state_of("r1").value == "pending_approval"
    assert await env.refunded() == Decimal("100.00")


@pytest.mark.edge
async def test_a_change_between_the_prompt_and_the_fingerprint_asks_again(
    env: Env,
) -> None:
    env.propose("r1", "200.00")
    env.enrol()
    session = env.session()
    before = _decide_by(env, session)
    options = env.client.post(
        "/v1/approvals/r1/options",
        json={"intent": "approve", "seen": env.seen()},
        headers=session,
    )
    signed = env.phone.sign(options.text)
    await env.paypal.refund_capture("CAP-1", Decimal("100.00"), "USD", "outside-1")
    first = env.client.post(
        "/v1/approvals/r1/approve", json={"credential": signed}, headers=session
    )
    assert first.json()["state"] == "pending_approval"  # asked again, nothing sent
    assert await env.refunded() == Decimal("100.00")
    assert _decide_by(env, session) > before  # the new question gets its own time
    second, _ = env.decide("r1", "approve", session)
    assert second.json()["state"] == "executed"
    assert await env.refunded() == Decimal("300.00")


@pytest.mark.edge
@pytest.mark.parametrize(
    "seen",
    [
        None,  # an approval never offered without the figures on screen
        {"paid": "500", "already_refunded": "-1.00"},
        {"paid": "500.001", "already_refunded": "0.00"},
        {"paid": "500.00", "already_refunded": "0.00", "extra": "x"},
    ],
)
def test_an_approval_prompt_needs_well_formed_figures_on_screen(
    env: Env, seen: dict[str, str] | None
) -> None:
    env.propose("r1", "30.00")
    env.enrol()
    response = env.client.post(
        "/v1/approvals/r1/options",
        json={"intent": "approve", "seen": seen},
        headers=env.session(),
    )
    assert response.status_code == (409 if seen is None else 422)
    if seen is None:
        assert "figures on screen are needed" in response.json()["detail"]


@pytest.mark.edge
async def test_an_approval_paypal_cannot_check_sends_nothing_and_stays_open(
    env: Env,
) -> None:
    env.propose("r1", "30.00")
    env.enrol()
    session = env.session()
    order = env.paypal._orders.pop("ORD-1")  # type: ignore[attr-defined]
    response, _ = env.decide("r1", "approve", session)
    env.paypal._orders["ORD-1"] = order  # type: ignore[attr-defined]
    assert response.status_code == 409
    assert _gateway(env)._ledger.state_of("r1").value == "pending_approval"
    assert await env.refunded() == Decimal("0")
    again, _ = env.decide("r1", "approve", session)  # PayPal is back: try again
    assert again.json()["state"] == "executed"


@pytest.mark.edge
async def test_a_refusal_at_approval_reads_as_owner_words_in_the_history(
    env: Env,
) -> None:
    env.propose("r1", "200.00")
    await env.paypal.refund_capture("CAP-1", Decimal("400.00"), "USD", "outside-1")
    env.enrol()
    session = env.session()
    outcome, _ = env.decide("r1", "approve", session)
    assert not re.search(r"\bR\d+\b", outcome.json()["merchant_reason"])
    record = env.client.get("/v1/approvals/r1/history", headers=session).json()
    steps = {event["step"]: event for event in record["events"]}
    assert "declined" not in steps  # the owner approved; they did not decline
    assert steps["refused_on_recheck"]["title"].startswith("Vikas approved")
    assert "No money moved" in steps["refused_on_recheck"]["detail"]
    # the re-decision is part of the sealed record, also for the full check
    assert record["sealed"] is True
    assert await _gateway(env).check_records_if_due() is not False
    again = env.client.get("/v1/approvals/r1/history", headers=session).json()
    assert again["sealed"] is True


@pytest.mark.edge
def test_a_stop_while_a_request_waits_does_not_move_its_deadline(env: Env) -> None:
    env.propose("r1", "30.00")
    env.enrol()
    session = env.session()
    before = _decide_by(env, session)
    ledger = _gateway(env)._ledger
    ledger.set_refunds_stopped(True, "Vikas", datetime.now(UTC))
    refused = env.client.post(
        "/v1/approvals/r1/options",
        json={"intent": "approve", "seen": env.seen()},
        headers=session,
    )
    assert refused.status_code == 409  # no fingerprint is even asked for
    ledger.set_refunds_stopped(False, "Vikas", datetime.now(UTC))
    assert _decide_by(env, session) == before


class LosesFirstRefundReply(RecordedPayPalClient):
    """PayPal applies the refund but the first reply never arrives."""

    def __init__(self) -> None:
        super().__init__({"ORD-1": order_json(captured="500.00")})
        self.calls = 0

    async def refund_capture(self, capture_id, amount, currency, request_id):  # type: ignore[no-untyped-def,override] -- test double
        self.calls += 1
        result = await super().refund_capture(capture_id, amount, currency, request_id)
        if self.calls == 1:
            raise PayPalClientError("timed out", {"retryable": True})
        return result


@pytest.mark.edge
async def test_a_lost_paypal_answer_is_shown_as_sending_then_finished_once(
    tmp_path: Path,
) -> None:
    env = Env(tmp_path)
    env.paypal = LosesFirstRefundReply()
    env.client = TestClient(create_app(env.settings, env.paypal))
    with env.client:
        env.propose("r1", "100.00")
        env.enrol()
        session = env.session()
        response, _ = env.decide("r1", "approve", session)
        assert response.json()["state"] == "executing"  # never claimed as sent
        listed = _listed(env, session)
        assert [i["status"] for i in listed] == ["sending"]
        gateway = env.client.app.state.gateway  # type: ignore[attr-defined]
        assert await gateway.finish_unfinished() == []  # too soon: no overlap
        later = datetime.now(UTC) + timedelta(seconds=RETRY_QUIET_SECONDS + 1)
        gateway._clock = lambda: later
        assert await gateway.finish_unfinished() == ["r1"]
        assert await env.refunded() == Decimal("100.00")  # once, not twice
        assert _listed(env, session) == []


# --- the repeat-refund pause, end to end -----------------------------------------
def _three_then_a_fourth(env: Env) -> Any:
    for n in range(1, 4):
        env.propose(f"r{n}", "10.00")
    return env.propose("r4", "10.00", order_id="ORD-1")


def test_a_fourth_refund_is_paused_with_a_kind_customer_message(env: Env) -> None:
    body = _three_then_a_fourth(env).json()
    assert body["state"] == "paused"
    assert body["update_by"]
    message = body["customer_message"]
    assert "We've received your refund request for order ORD-1" in message
    assert "You don't need to do anything" in message
    for word in ("limit", "denied", "fraud", "abuse", "3 refunds"):
        assert word not in message.lower()


@pytest.mark.edge
def test_nobody_can_send_a_paused_refund_but_declining_works(env: Env) -> None:
    _three_then_a_fourth(env)
    env.enrol()
    session = env.session()
    listed = _listed(env, session)
    paused = [i for i in listed if i["status"] == "paused"]
    assert paused and paused[0]["count_in_window"] == "4"
    approve = env.client.post(
        "/v1/approvals/r4/options",
        json={"intent": "approve", "seen": env.seen()},
        headers=session,
    )
    assert approve.status_code == 409  # sending early is not possible
    response, _ = env.decide("r4", "reject", session)
    assert response.json()["state"] == "denied"
    status = env.client.get("/v1/proposals/r4", headers=env.key("agent_api_key"))
    assert "can't make a refund" in status.json()["customer_message"]


@pytest.mark.edge
def test_asking_again_returns_the_same_pause_not_a_new_request(env: Env) -> None:
    _three_then_a_fourth(env)
    again = env.propose("r4-again", "10.00").json()
    assert again["request_id"] == "r4"
    assert again["replayed"] is True
    assert "saved and on track" in again["customer_message"]


async def test_a_pause_is_decided_again_from_fresh_facts_at_release(env: Env) -> None:
    _three_then_a_fourth(env)
    gateway = env.client.app.state.gateway  # type: ignore[attr-defined]
    assert await gateway.release_due() == []  # not before the release time
    later = datetime.now(UTC) + timedelta(hours=25)
    gateway._clock = lambda: later
    assert await gateway.release_due() == ["r4"]
    status = env.client.get("/v1/proposals/r4", headers=env.key("agent_api_key")).json()
    # back under the limit; the safety check is not wired yet, so a person decides
    assert status["state"] == "pending_approval"
    assert "with our team now" in status["customer_message"]


@pytest.mark.edge
async def test_an_unreachable_paypal_keeps_a_pause_paused(env: Env) -> None:
    _three_then_a_fourth(env)
    gateway = env.client.app.state.gateway  # type: ignore[attr-defined]
    later = datetime.now(UTC) + timedelta(hours=25)
    gateway._clock = lambda: later
    env.paypal._orders.pop("ORD-1")  # type: ignore[attr-defined]  # PayPal cannot answer
    assert await gateway.release_due() == []
    status = env.client.get("/v1/proposals/r4", headers=env.key("agent_api_key"))
    assert status.json()["state"] == "paused"


# The on-hold card shows the line the customer was given. Only whether it greeted
# them by name is kept, never the name: a name used and still readable, a name
# used but PayPal unreachable now, and no name used though PayPal has one now.
def _paused_card(env: Env) -> dict[str, Any]:
    env.enrol()
    home = _home(env, env.session())
    card: dict[str, Any] = home["sections"]["paused"][0]
    return card


@pytest.mark.edge
def test_the_on_hold_card_shows_the_greeting_the_customer_was_given(
    env: Env,
) -> None:
    _three_then_a_fourth(env)
    card = _paused_card(env)
    assert card["customer_message"].startswith("Thanks, Jane.")
    assert card["customer_message_exact"] == "yes"


@pytest.mark.edge
def test_a_name_paypal_cannot_give_now_is_flagged_not_swapped(env: Env) -> None:
    _three_then_a_fourth(env)
    env.paypal._orders.pop("ORD-1")  # type: ignore[attr-defined]
    card = _paused_card(env)
    assert card["customer_message"].startswith("Thanks. ")
    assert card["customer_message_exact"] == "no"  # the customer saw their name


@pytest.mark.edge
async def test_a_line_given_without_a_name_is_never_shown_with_one(
    env: Env, monkeypatch: pytest.MonkeyPatch
) -> None:
    gateway = _gateway(env)

    async def no_name(order_id: str | None) -> None:
        return None  # PayPal could not give a name when the customer was told

    with monkeypatch.context() as patch:
        patch.setattr(gateway, "_first_name", no_name)
        assert _three_then_a_fourth(env).json()["state"] == "paused"
    card = _paused_card(env)  # PayPal now has the name
    assert card["customer_message"].startswith("Thanks. ")
    assert card["customer_message_exact"] == "yes"


@pytest.mark.parametrize("earlier", [3, 4])
@pytest.mark.edge
def test_the_paused_card_counts_the_real_refunds_in_the_window(
    env: Env, earlier: int
) -> None:
    # found on the phone: the card said "4th" when the customer already had four
    # refunds, made before the pause existed (or before the limit was lowered)
    for n in range(1, earlier + 1):
        env.propose(f"r{n}", f"{n}.00")
    if earlier > 3:
        with sqlite3.connect(env.settings.sqlite_db_path) as db:
            db.execute(
                "UPDATE proposals SET state = 'executed' WHERE request_id = 'r4'"
            )
    env.propose("next", "9.00")
    env.enrol()
    listed = _listed(env, env.session())
    paused = [
        i for i in listed if i["status"] == "paused" and i["request_id"] == "next"
    ]
    assert paused[0]["count_in_window"] == str(earlier + 1)


# --- a refund PayPal accepts as pending and later fails ---------------------------
@pytest.mark.edge
async def test_a_pending_refund_that_fails_later_is_never_reported_as_sent(
    tmp_path: Path,
) -> None:
    env = Env(tmp_path)
    env.paypal = RecordedPayPalClient(
        {"ORD-1": order_json(captured="500.00")}, refund_status="PENDING"
    )
    env.client = TestClient(create_app(env.settings, env.paypal))
    with env.client:
        env.propose("r1", "10.00")
        env.enrol()
        session = env.session()
        response, _ = env.decide("r1", "approve", session)
        assert response.json()["paypal_status"] == "PENDING"
        gateway = env.client.app.state.gateway  # type: ignore[attr-defined]
        assert await gateway.settle_pending() == []  # still pending: nothing to do
        env.paypal.set_refund_status("r1", "FAILED")
        assert await gateway.settle_pending() == ["r1"]
        agent = env.key("agent_api_key")
        status = env.client.get("/v1/proposals/r1", headers=agent).json()
        assert status["state"] == "failed"
        assert "didn't go through" in status["customer_message"]
        listed = _listed(env, session)
        assert [i["status"] for i in listed] == ["failed_late"]


async def test_a_pending_refund_that_completes_is_settled(tmp_path: Path) -> None:
    env = Env(tmp_path)
    env.paypal = RecordedPayPalClient(
        {"ORD-1": order_json(captured="500.00")}, refund_status="PENDING"
    )
    env.client = TestClient(create_app(env.settings, env.paypal))
    with env.client:
        env.propose("r1", "10.00")
        env.enrol()
        env.decide("r1", "approve", env.session())
        gateway = env.client.app.state.gateway  # type: ignore[attr-defined]
        env.paypal.set_refund_status("r1", "COMPLETED")
        assert await gateway.settle_pending() == ["r1"]
        status = env.client.get("/v1/proposals/r1", headers=env.key("agent_api_key"))
        assert status.json()["state"] == "executed"
        assert await gateway.settle_pending() == []  # nothing left to re-read


# --- edge cases where real decisions happen ----------------------------------------
def _gateway(env: Env) -> Any:
    return env.client.app.state.gateway  # type: ignore[attr-defined]


@pytest.mark.edge
async def test_e1_two_pauses_releasing_together_never_both_pass(env: Env) -> None:
    _three_then_a_fourth(env)  # the fourth request is paused
    env.propose("r5", "11.00")  # a different amount: its own pause
    later = datetime.now(UTC) + timedelta(hours=24, minutes=1)
    _gateway(env)._clock = lambda: later
    await _gateway(env).release_due()
    agent = env.key("agent_api_key")
    states = {
        rid: env.client.get(f"/v1/proposals/{rid}", headers=agent).json()["state"]
        for rid in ("r4", "r5")
    }
    # the three earlier refunds left the window, so both fit; neither is lost
    assert set(states.values()) <= {"pending_approval", "paused"}
    assert "pending_approval" in states.values()


@pytest.mark.edge
async def test_e2_an_order_refunded_elsewhere_while_paused_is_refused_at_release(
    env: Env,
) -> None:
    _three_then_a_fourth(env)
    await env.paypal.refund_capture("CAP-1", Decimal("500.00"), "USD", "outside-all")
    later = datetime.now(UTC) + timedelta(hours=25)
    _gateway(env)._clock = lambda: later
    assert await _gateway(env).release_due() == ["r4"]
    status = env.client.get("/v1/proposals/r4", headers=env.key("agent_api_key"))
    assert status.json()["state"] == "denied"
    assert "can't make a refund" in status.json()["customer_message"]
    assert await env.refunded() == Decimal("500.00")  # nothing more went out


@pytest.mark.edge
def test_e4_same_order_different_amount_is_its_own_request(env: Env) -> None:
    _three_then_a_fourth(env)
    other = env.propose("r4-other", "12.00").json()
    assert other["request_id"] == "r4-other"
    assert other["replayed"] is False
    assert other["state"] == "paused"


@pytest.mark.edge
def test_e5_asking_again_after_a_decline_is_a_fresh_request(env: Env) -> None:
    _three_then_a_fourth(env)
    env.enrol()
    env.decide("r4", "reject", env.session())
    again = env.propose("r4-again", "10.00").json()
    assert again["request_id"] == "r4-again"  # the declined one is not revived
    assert again["state"] == "paused"


@pytest.mark.edge
async def test_e6_a_decline_racing_the_release_wins_and_others_still_release(
    env: Env,
) -> None:
    _three_then_a_fourth(env)
    env.propose("r5", "11.00")
    env.enrol()
    env.decide("r4", "reject", env.session())
    gateway = _gateway(env)
    real = gateway._ledger.releases_due
    gateway._ledger.releases_due = lambda now: ["r4", *real(now)]  # stale list
    later = datetime.now(UTC) + timedelta(hours=25)
    gateway._clock = lambda: later
    assert await gateway.release_due() == [
        "r5"
    ]  # the declined one is skipped, the other released
    status = env.client.get("/v1/proposals/r4", headers=env.key("agent_api_key"))
    assert status.json()["state"] == "denied"


@pytest.mark.edge
async def test_e9_an_immediate_paypal_failure_is_never_shown_as_processing(
    tmp_path: Path,
) -> None:
    env = Env(tmp_path)
    env.paypal = RecordedPayPalClient(
        {"ORD-1": order_json(captured="500.00")}, refund_status="FAILED"
    )
    env.client = TestClient(create_app(env.settings, env.paypal))
    with env.client:
        env.propose("r1", "10.00")
        env.enrol()
        response, _ = env.decide("r1", "approve", env.session())
        assert response.json()["state"] == "failed"
        status = env.client.get("/v1/proposals/r1", headers=env.key("agent_api_key"))
        assert "didn't go through" in status.json()["customer_message"]


@pytest.mark.edge
async def test_a_declined_counted_request_releases_the_pause_early(env: Env) -> None:
    # found on the phone: the pause waited 24 hours although declining one of the
    # counted requests brought the customer back under the limit
    _three_then_a_fourth(env)
    env.enrol()
    env.decide("r1", "reject", env.session())
    # re-decided as part of the decline itself, not tomorrow and not on a later
    # background pass, so the screen never shows a stale pause
    status = env.client.get("/v1/proposals/r4", headers=env.key("agent_api_key"))
    assert status.json()["state"] == "pending_approval"
    assert await _gateway(env).release_due() == []


@pytest.mark.edge
async def test_a_released_pause_still_passes_the_audit_integrity_check(
    env: Env,
) -> None:
    # found on the device run: rows of released pauses failed the check that
    # rebuilds every row from the audit trail, so tampering could not be told
    # apart from an honest release
    _three_then_a_fourth(env)
    env.enrol()
    env.decide("r1", "reject", env.session())  # releases the pause at once
    ledger = _gateway(env)._ledger
    assert ledger.state_of("r4").value == "pending_approval"
    assert ledger.verify_records() >= 4


@pytest.mark.edge
async def test_editing_a_released_pause_is_still_detected(env: Env) -> None:
    _three_then_a_fourth(env)
    env.enrol()
    env.decide("r1", "reject", env.session())
    with sqlite3.connect(env.settings.sqlite_db_path) as db:
        db.execute("UPDATE proposals SET decision = 'allow' WHERE request_id = 'r4'")
    with pytest.raises(AuditIntegrityError):
        _gateway(env)._ledger.verify_records()


# --- the merchant's own app: who, their day, their record ---------------------------
def test_signing_in_returns_the_merchant_name_and_account(env: Env) -> None:
    env.enrol()
    options = env.client.post("/v1/approver/session/options").text
    body = env.client.post(
        "/v1/approver/session", json={"credential": env.phone.sign(options)}
    ).json()
    assert body["approver"] == "Vikas"
    assert body["account"] == ACCOUNT[-4:]


@pytest.mark.edge
async def test_the_day_summary_counts_only_what_really_happened(env: Env) -> None:
    env.propose("r1", "10.00")
    env.propose("r2", "12.00")
    env.propose("r3", "14.00")
    env.enrol()
    session = env.session()
    env.decide("r1", "approve", session)
    env.decide("r2", "reject", session)
    home = _home(env, session)
    day = {**home["today"], **home["settings"]}
    assert day["sent_count"] == "1" and day["sent_total"] == "10.00"
    assert day["by_you_count"] == "1" and day["by_assistant_count"] == "0"
    assert day["not_sent_count"] == "1"
    # the waiting 14.00 is money set aside, so it counts against the daily limit
    assert day["limit_used"] == "24.00"
    assert day["auto_limit"] and day["daily_cap"] and day["repeat_limit"]


@pytest.mark.edge
async def test_history_never_claims_the_customer_was_told(env: Env) -> None:
    env.propose("r1", "10.00")
    env.enrol()
    session = env.session()
    env.decide("r1", "approve", session)
    record = env.client.get("/v1/approvals/r1/history", headers=session).json()
    titles = [e["title"] for e in record["events"]]
    assert titles[0] == "Your AI assistant asked"
    assert "Vikas approved" in titles
    assert "Refunded by PayPal" in titles
    assert not any("customer" in t.lower() and "told" in t.lower() for t in titles)
    assert record["sealed"] is True


@pytest.mark.edge
async def test_history_says_unsealed_when_the_record_was_edited(env: Env) -> None:
    env.propose("r1", "10.00")
    env.enrol()
    session = env.session()
    with sqlite3.connect(env.settings.sqlite_db_path) as db:
        db.execute("UPDATE proposals SET amount = '1.00' WHERE request_id = 'r1'")
    record = env.client.get("/v1/approvals/r1/history", headers=session).json()
    assert record["sealed"] is False


@pytest.mark.edge
async def test_a_break_found_by_the_full_check_marks_every_history(env: Env) -> None:
    env.propose("r1", "30.00")
    env.propose("r2", "30.00")
    env.enrol()
    session = env.session()
    gateway = _gateway(env)
    assert await gateway.check_records_if_due() is True  # never run: due now
    _db(env, "UPDATE audit SET body = replace(body, '30.00', '3.00') WHERE seq = 1")
    history = "/v1/approvals/r2/history"
    assert env.client.get(history, headers=session).json()["sealed"] is True
    # the next full check is due (moving the clock would also end the session)
    gateway._last_full_check = datetime.now(UTC) - timedelta(
        hours=env.settings.audit.full_check_hours, seconds=1
    )
    assert await gateway.check_records_if_due() is False  # ran, found the edit
    assert env.client.get(history, headers=session).json()["sealed"] is False


@pytest.mark.edge
async def test_the_full_check_runs_only_when_due(
    env: Env, monkeypatch: pytest.MonkeyPatch
) -> None:
    gateway = _gateway(env)
    runs: list[int] = []
    real = gateway._ledger.verify_records
    monkeypatch.setattr(
        gateway._ledger, "verify_records", lambda: runs.append(1) or real()
    )
    await gateway.check_records_if_due()
    await gateway.check_records_if_due()  # moments later: not due
    assert runs == [1]
    hours = env.settings.audit.full_check_hours
    start = datetime.now(UTC)
    gateway._clock = lambda: start + timedelta(hours=hours) - timedelta(seconds=1)
    assert await gateway.check_records_if_due() is None  # one second early
    gateway._clock = lambda: start + timedelta(hours=hours, seconds=1)
    await gateway.check_records_if_due()
    assert runs == [1, 1]


@pytest.mark.edge
async def test_a_blocked_card_separates_outside_refunds(env: Env) -> None:
    env.propose("r1", "10.00")
    env.enrol()
    session = env.session()
    env.decide("r1", "approve", session)
    env.propose("r2", "20.00")
    await env.paypal.refund_capture("CAP-1", Decimal("480.00"), "USD", "outside-1")
    listed = _listed(env, session)
    card = next(i for i in listed if i["request_id"] == "r2")
    assert card["fits"] == "no"
    assert card["refunded_in_app"] == "10.00"
    assert card["refunded_outside"] == "480.00"


def test_done_today_lists_what_ended_with_its_outcome(env: Env) -> None:
    env.propose("r1", "10.00")
    env.enrol()
    session = env.session()
    env.decide("r1", "reject", session)
    done = env.client.get("/v1/approvals/done", headers=session).json()["items"]
    assert [(d["request_id"], d["outcome"]) for d in done] == [("r1", "denied")]
    assert done[0]["customer"] == "Jane Buyer"


@pytest.mark.edge
def test_an_expired_request_never_tells_the_customer_no(env: Env) -> None:
    env.propose("r1", "10.00")
    gateway = _gateway(env)
    later = datetime.now(UTC) + timedelta(seconds=gateway._open_seconds + 1)
    gateway._clock = lambda: later
    gateway.expire()
    status = env.client.get("/v1/proposals/r1", headers=env.key("agent_api_key"))
    assert status.json()["state"] == "expired"
    message = status.json()["customer_message"]
    assert "wasn't confirmed in time" in message and "can't make" not in message


def test_a_paused_card_shows_what_the_customer_was_told(env: Env) -> None:
    _three_then_a_fourth(env)
    env.enrol()
    listed = _listed(env, env.session())
    paused = next(i for i in listed if i["status"] == "paused")
    assert "We've received your refund request" in paused["customer_message"]
    assert paused["order_ending"] == "RD-1"[-4:]


# --- the home screen: the server decides, the phone only shows ---------------------
def _home(env: Env, session: dict[str, str]) -> dict[str, Any]:
    response = env.client.get("/v1/approvals/home", headers=session)
    assert response.status_code == 200
    return response.json()


def _ids(home: dict[str, Any], section: str) -> list[str]:
    return [item["request_id"] for item in home["sections"][section]]


@pytest.mark.edge
async def test_home_puts_each_request_in_the_section_the_server_decides(
    env: Env,
) -> None:
    env.propose("r1", "10.00")
    env.propose("r2", "20.00")
    env.enrol()
    session = env.session()
    await env.paypal.refund_capture("CAP-1", Decimal("475.00"), "USD", "outside-1")
    home = _home(env, session)
    # 25.00 is left and each request counts the other as held, so neither fits
    assert sorted(_ids(home, "cant_send")) == ["r1", "r2"]
    assert all(i["can_send"] == "no" for i in home["sections"]["cant_send"])
    assert home["sections"]["needs_decision"] == []
    assert home["counts"] == {
        "needs_decision": "0",
        "cant_send": "2",
        "paused": "0",
        "sending": "0",
        "failed": "0",
    }


@pytest.mark.edge
def test_home_never_offers_send_when_paypal_cannot_be_read(env: Env) -> None:
    env.propose("r1", "10.00")
    env.enrol()
    session = env.session()
    env.paypal._orders.pop("ORD-1")  # PayPal stops answering for this order
    home = _home(env, session)
    assert _ids(home, "cant_send") == ["r1"]
    assert home["sections"]["cant_send"][0]["can_send"] == "no"
    assert home["paypal_checked"] == "no"
    assert home["checked_at"] == ""  # never claim a check that did not happen
    assert home["sections"]["cant_send"][0]["checked_at"] == ""


# Freshness edge cases: an empty home, only refunds already with PayPal (never
# read), a read that fails on an on-hold card while a waiting card reads fine,
# and the detail screen showing the card's own check time.
@pytest.mark.edge
def test_an_empty_home_claims_no_check_with_paypal(env: Env) -> None:
    env.enrol()
    home = _home(env, env.session())
    assert home["paypal_checked"] == "none"
    assert home["checked_at"] == ""


@pytest.mark.edge
async def test_refunds_already_with_paypal_carry_no_check_time(
    tmp_path: Path,
) -> None:
    env = Env(tmp_path)
    env.paypal = LosesFirstRefundReply()
    env.client = TestClient(create_app(env.settings, env.paypal))
    with env.client:
        env.propose("r1", "100.00")
        env.enrol()
        session = env.session()
        env.decide("r1", "approve", session)
        home = _home(env, session)
        assert _ids(home, "sending") == ["r1"]
        assert home["sections"]["sending"][0]["checked_at"] == ""
        assert home["paypal_checked"] == "none"
        assert home["checked_at"] == ""


@pytest.mark.edge
def test_a_failed_read_on_an_on_hold_card_is_not_hidden(env: Env) -> None:
    # the same card on a second order, so only the on-hold card's read fails
    env.paypal._orders["ORD-3"] = order_json(  # type: ignore[attr-defined]
        order="ORD-3", capture="CAP-3", captured="500.00"
    )
    for n in range(1, 4):
        env.propose(f"r{n}", "10.00")
    assert env.propose("r4", "10.00", order_id="ORD-3").json()["state"] == "paused"
    env.enrol()
    session = env.session()
    env.paypal._orders.pop("ORD-3")  # type: ignore[attr-defined]
    home = _home(env, session)
    others = [
        i for s in home["sections"].values() for i in s if i["request_id"] != "r4"
    ]
    assert others and all(i["checked_at"] for i in others)
    assert home["sections"]["paused"][0]["checked_at"] == ""
    assert home["paypal_checked"] == "partial"
    assert home["checked_at"] == ""


@pytest.mark.edge
def test_a_late_failure_whose_read_fails_is_counted(
    env: Env, monkeypatch: pytest.MonkeyPatch
) -> None:
    env.enrol()
    session = env.session()
    gateway = _gateway(env)
    late = {
        "request_id": "r9",
        "tool": "create_refund",
        "amount": "10.00",
        "currency": "USD",
        "order_id": "ORD-GONE",
        "updated_at": datetime.now(UTC).isoformat(),
    }
    monkeypatch.setattr(gateway._ledger, "late_failures", lambda now: [late])
    home = _home(env, session)
    assert home["paypal_checked"] == "no"
    assert home["sections"]["failed"][0]["checked_at"] == ""


def test_the_detail_screen_shows_the_cards_own_check_time() -> None:
    review = _script_function("openReview")
    assert "item.checked_at" in review
    assert "home.checked_at" not in review


@pytest.mark.edge
def test_home_past_the_deadline_is_not_sendable_before_expiry_runs(env: Env) -> None:
    env.propose("r1", "10.00")
    env.enrol()
    session = env.session()
    gateway = _gateway(env)
    gateway._open_seconds = 0  # the window to decide has already closed
    home = _home(env, session)
    item = next(i for s in home["sections"].values() for i in s)
    assert item["can_send"] == "no"
    assert item["cant_send_reason"] == "time_up"
    assert datetime.fromisoformat(home["server_now"]) <= datetime.now(UTC)


@pytest.mark.edge
def test_no_fingerprint_is_asked_for_an_approval_past_its_deadline(env: Env) -> None:
    env.propose("r1", "30.00")
    env.enrol()
    session = env.session()
    _gateway(env)._open_seconds = 0  # the window has closed; the expiry job has not run
    options = env.client.post(
        "/v1/approvals/r1/options",
        json={"intent": "approve", "seen": env.seen()},
        headers=session,
    )
    assert options.status_code == 409


@pytest.mark.edge
async def test_a_fingerprint_that_arrives_after_the_deadline_sends_nothing(
    env: Env,
) -> None:
    env.propose("r1", "30.00")
    env.enrol()
    session = env.session()
    options = env.client.post(
        "/v1/approvals/r1/options",
        json={"intent": "approve", "seen": env.seen()},
        headers=session,
    ).text
    signed = env.phone.sign(options)  # signed in time
    gateway = _gateway(env)
    gateway._open_seconds = 0  # answer arrives after the deadline, before expiry
    response = env.client.post(
        "/v1/approvals/r1/approve", json={"credential": signed}, headers=session
    )
    assert response.status_code == 409
    assert await env.refunded() == Decimal("0")
    assert gateway._ledger.state_of("r1").value == "pending_approval"


@pytest.mark.edge
async def test_the_deadline_is_checked_again_where_the_approval_is_written(
    env: Env, monkeypatch: pytest.MonkeyPatch
) -> None:
    env.propose("r1", "30.00")
    env.enrol()
    session = env.session()
    options = env.client.post(
        "/v1/approvals/r1/options",
        json={"intent": "approve", "seen": env.seen()},
        headers=session,
    ).text
    signed = env.phone.sign(options)
    gateway = _gateway(env)
    gateway._open_seconds = 0
    # the deadline passes between the early check and the write
    monkeypatch.setattr(gateway, "_past_deadline", lambda request_id: False)
    response = env.client.post(
        "/v1/approvals/r1/approve", json={"credential": signed}, headers=session
    )
    assert response.status_code == 409
    assert await env.refunded() == Decimal("0")


@pytest.mark.edge
async def test_the_deadline_is_checked_after_the_paypal_read_at_approval(
    env: Env, monkeypatch: pytest.MonkeyPatch
) -> None:
    env.propose("r1", "30.00")
    env.enrol()
    session = env.session()
    options = env.client.post(
        "/v1/approvals/r1/options",
        json={"intent": "approve", "seen": env.seen()},
        headers=session,
    ).text
    signed = env.phone.sign(options)
    gateway = _gateway(env)
    real_read = gateway._evidence

    async def slow_read(proposal: Any, now: datetime) -> Any:
        found = await real_read(proposal, now)
        later = datetime.now(UTC) + timedelta(seconds=gateway._open_seconds + 1)
        gateway._clock = lambda: later  # the deadline passes during the read
        return found

    monkeypatch.setattr(gateway, "_evidence", slow_read)
    response = env.client.post(
        "/v1/approvals/r1/approve", json={"credential": signed}, headers=session
    )
    assert response.status_code == 409
    assert await env.refunded() == Decimal("0")
    # the signed approval that arrived too late is still on the record
    with sqlite3.connect(gateway._ledger.path) as conn:
        events = [
            r[0]
            for r in conn.execute("SELECT event FROM audit WHERE request_id = 'r1'")
        ]
    assert "approval_not_applied" in events


@pytest.mark.edge
async def test_after_a_figures_changed_refusal_home_shows_the_new_figures(
    env: Env,
) -> None:
    env.propose("r1", "200.00")
    env.enrol()
    session = env.session()
    on_screen = env.seen()
    _listed(env, session)  # the phone's last read of the order
    await env.paypal.refund_capture("CAP-1", Decimal("100.00"), "USD", "outside-1")
    refused, _ = env.decide("r1", "approve", session, seen=on_screen)
    assert refused.status_code == 409
    card = next(i for i in _listed(env, session) if i["request_id"] == "r1")
    assert card["already_refunded"] == "100.00"  # not the read from before


@pytest.mark.edge
async def test_a_failed_note_never_hides_why_an_approval_was_refused(
    env: Env, monkeypatch: pytest.MonkeyPatch
) -> None:
    env.propose("r1", "30.00")
    env.enrol()
    session = env.session()
    prompt = env.client.post(
        "/v1/approvals/r1/options",
        json={"intent": "approve", "seen": env.seen()},
        headers=session,
    )
    signed = env.phone.sign(prompt.text)
    gateway = _gateway(env)
    real_read = gateway._evidence

    async def slow_read(proposal: Any, now: datetime) -> Any:
        found = await real_read(proposal, now)
        later = datetime.now(UTC) + timedelta(seconds=gateway._open_seconds + 1)
        gateway._clock = lambda: later  # the deadline passes during the read
        return found

    def locked(*_args: Any) -> None:
        raise sqlite3.OperationalError("database is locked")

    monkeypatch.setattr(gateway, "_evidence", slow_read)
    monkeypatch.setattr(gateway._ledger, "record_unapplied_approval", locked)
    response = env.client.post(
        "/v1/approvals/r1/approve", json={"credential": signed}, headers=session
    )
    assert response.status_code == 409  # the refusal, not a server error
    assert await env.refunded() == Decimal("0")


@pytest.mark.edge
def test_an_approval_paypal_could_not_check_is_still_on_the_record(env: Env) -> None:
    env.propose("r1", "30.00")
    env.enrol()
    session = env.session()
    reads = iter([True, True])  # screen figures and the check before the fingerprint
    real_get = env.paypal.get_order

    async def flaky(order_id: str) -> Any:
        if next(reads, False):
            return await real_get(order_id)
        raise PayPalClientError("unreachable")

    env.paypal.get_order = flaky  # type: ignore[method-assign]
    response, _ = env.decide("r1", "approve", session)
    env.paypal.get_order = real_get  # type: ignore[method-assign]
    assert response.status_code == 409
    ledger = _gateway(env)._ledger
    with sqlite3.connect(ledger.path) as conn:
        events = [
            r[0]
            for r in conn.execute("SELECT event FROM audit WHERE request_id = 'r1'")
        ]
    assert "approval_not_applied" in events
    ledger.verify_request("r1")


# A fingerprint that cannot be accepted is told apart from a request that cannot
# be decided: unknown passkey, no user verification, wrong site, and a stop
# signature replayed to resume.
def _signed_by(
    env: Env, session: dict[str, str], phone: SoftAuthenticator, **how: Any
) -> Any:
    options = env.client.post(
        "/v1/approvals/r1/options",
        json={"intent": "approve", "seen": env.seen()},
        headers=session,
    ).text
    return env.client.post(
        "/v1/approvals/r1/approve",
        json={"credential": phone.sign(options, **how)},
        headers=session,
    )


@pytest.mark.edge
@pytest.mark.parametrize(
    ("stranger", "how"),
    [
        (True, {}),  # a passkey this merchant never set up
        (False, {"verified": False}),  # no fingerprint or PIN behind it
        (False, {"origin": "https://elsewhere.example"}),  # signed for another site
    ],
)
async def test_a_fingerprint_that_cannot_be_accepted_is_not_a_closed_request(
    env: Env, stranger: bool, how: dict[str, Any]
) -> None:
    env.propose("r1", "30.00")
    env.enrol()
    session = env.session()
    phone = SoftAuthenticator("localhost", ORIGIN) if stranger else env.phone
    response = _signed_by(env, session, phone, **how)
    assert response.status_code == 403
    assert _gateway(env)._ledger.state_of("r1").value == "pending_approval"
    assert await env.refunded() == Decimal("0")


@pytest.mark.edge
@pytest.mark.parametrize(
    ("home", "latest", "title"),
    [
        ({"refunds_stopped": "yes"}, None, "Refunds are stopped"),
        (
            {"refunds_stopped": "no"},
            {"cant_send_reason": "time_up"},
            "Time ran out. No money moved.",
        ),
        ({"refunds_stopped": "no"}, None, "This refund request is already closed."),
        (
            {"refunds_stopped": "no"},
            {"cant_send_reason": "not_enough_left"},
            "This refund request is already closed.",
        ),
        (  # PayPal could not be read for the check at approval
            {"refunds_stopped": "no"},
            {"cant_send_reason": "paypal_unavailable"},
            "Couldn't reach PayPal. No money moved.",
        ),
        (  # PayPal answered again by the time the phone re-read
            {"refunds_stopped": "no"},
            {"status": "waiting", "can_send": "yes"},
            "That didn't go through. No money moved.",
        ),
    ],
)
def test_a_refused_decision_says_why_from_a_fresh_read(
    home: dict[str, str], latest: dict[str, str] | None, title: str
) -> None:
    expression = f"refusedOutcome({json.dumps(home)}, {json.dumps(latest)})"
    assert _run_js(["refusedOutcome"], expression)[1] == title


# --- deadlines on the phone: a time of day, no countdown, exact at the end -------
DEADLINE_MS = 1_800_000_000_000  # a fixed moment, so the tests never race a clock


@pytest.mark.edge
@pytest.mark.parametrize(
    ("seconds_left", "starts"),
    [
        (3_600, "Decide by"),
        (89, "Decide by"),
        (30, "Decide by"),  # rounding once showed "1 min left" here
        (29, "Decide by"),  # and "Time ran out" here, with time still left
        (1, "Decide by"),
        (0, "Time ran out"),  # closed at its last moment, like the server
        (-1, "Time ran out"),
    ],
)
def test_a_card_says_when_it_closes_and_flips_exactly_at_the_end(
    seconds_left: int, starts: str
) -> None:
    deadline = datetime.fromtimestamp(DEADLINE_MS / 1000, UTC).isoformat()
    now = DEADLINE_MS - seconds_left * 1000
    text = _run_js(
        ["deadlineText", "closingTime", "clock"],
        f"deadlineText({json.dumps(deadline)}, {now})",
        constants=("MS_PER_DAY",),
    )
    assert text.startswith(starts)
    assert "min left" not in text


@pytest.mark.edge
def test_a_card_closing_tomorrow_says_so() -> None:
    deadline = datetime.fromtimestamp(DEADLINE_MS / 1000, UTC)
    now = (deadline - timedelta(days=1)).timestamp() * 1000
    text = _run_js(
        ["deadlineText", "closingTime", "clock"],
        f"deadlineText({json.dumps(deadline.isoformat())}, {now})",
        constants=("MS_PER_DAY",),
    )
    assert text.startswith("Decide by tomorrow")


@pytest.mark.edge
@pytest.mark.parametrize(
    ("offsets", "expected"),
    [
        ([-5_000, 5_000, 1_000], 1_000),  # the soonest still open, past ones ignored
        ([-1, 0], None),  # nothing left to wait for
        ([], None),
    ],
)
def test_the_phone_re_reads_when_the_soonest_request_closes(
    offsets: list[int], expected: int | None
) -> None:
    items = [
        {"decide_by": datetime.fromtimestamp((DEADLINE_MS + o) / 1000, UTC).isoformat()}
        for o in offsets
    ]
    wait = _run_js(
        ["msUntilSoonest"], f"msUntilSoonest({json.dumps(items)}, {DEADLINE_MS})"
    )
    assert wait == expected


_SEEN = {
    "section": "needs_decision",
    "can_send": "yes",
    "amount": "60.00",
    "paid": "100.00",
    "already_refunded": "0.00",
    "refundable_now": "100.00",
    "waiting_on_order": "0.00",
    "paypal_read": "yes",
}


@pytest.mark.edge
@pytest.mark.parametrize(
    ("latest", "verdict"),
    [
        (_SEEN, "same"),
        # refunded in PayPal directly while the screen was open
        ({**_SEEN, "already_refunded": "20.00", "refundable_now": "80.00"}, "changed"),
        ({**_SEEN, "can_send": "no", "section": "cant_send"}, "changed"),
        (None, "closed"),  # decided or expired elsewhere
        ({**_SEEN, "paypal_read": "no"}, "unchecked"),  # PayPal did not answer
    ],
)
def test_the_final_screen_checks_its_figures_with_paypal_before_the_fingerprint(
    latest: dict[str, str] | None, verdict: str
) -> None:
    found = _run_js(
        ["freshVerdict", "fingerprint"],
        f"freshVerdict({json.dumps(_SEEN)}, {json.dumps(latest)})",
    )
    assert found == verdict


@pytest.mark.edge
def test_the_fingerprint_button_waits_for_the_fresh_check() -> None:
    """Wiring only: the verdicts are tested by running them above."""
    final = _script_function("openFinal")
    assert "confirmFresh(item)" in final
    fresh = _script_function("confirmFresh")
    assert "send.disabled = true" in fresh
    assert "freshVerdict(item, latest)" in fresh
    assert "latest = findItem(item.request_id)" in fresh


@pytest.mark.edge
def test_unconfirmed_figures_never_stay_under_a_change_or_a_failed_check() -> None:
    """Wiring only: a banner or a failed read removes the old figures, the send
    warning and the checking line, so the first thing read is true."""
    final = _script_function("openFinal")
    assert final.count("unconfirmed") >= 2  # the figures table and the send warning
    banner = _script_function("changeBanner")
    assert "dropUnconfirmed(screen)" in banner
    assert 'setProgress(screen, "", false)' in banner
    fresh = _script_function("confirmFresh")
    assert fresh.count("dropUnconfirmed(screen)") == 2  # not read, read failed
    assert "box.remove()" in _script_function("dropUnconfirmed")


@pytest.mark.edge
def test_a_hold_with_no_line_given_never_shows_one(env: Env) -> None:
    item = {
        "greeting_named": "none",
        "order_id": "ORD-1",
        "amount": "60.00",
        "release_at": "2026-11-03T12:00:00+00:00",
        "created_at": "2026-11-02T12:00:00+00:00",
    }
    line = _gateway(env)._paused_line(item, {"customer": "Jane Buyer"})
    assert line == {"customer_message": "", "customer_message_exact": "none"}


@pytest.mark.edge
@pytest.mark.parametrize(
    ("exact", "heading", "note"),
    [
        ("yes", "Message given to your assistant for the customer", ""),
        (
            "no",
            "Message given to your assistant for the customer",
            "Couldn't confirm the customer's name in this message.",
        ),
        ("none", "Message for the customer", ""),  # nothing was given at the hold
    ],
)
def test_the_hold_screen_only_quotes_a_line_that_was_given(
    exact: str, heading: str, note: str
) -> None:
    item = {"customer_message_exact": exact, "customer_message": "Thanks, Jane."}
    view = _run_js(
        ["customerLineView", "customerLineNote"],
        f"customerLineView({json.dumps(item)})",
    )
    assert view[0] == heading
    assert view[2] == note
    if exact == "none":
        assert view[1] == "No message was given to the customer when this went on hold."
    else:
        assert view[1] == "Thanks, Jane."


@pytest.mark.edge
def test_the_phone_sends_the_figures_on_screen_with_an_approval() -> None:
    """Wiring only: the server compares these with PayPal before any fingerprint."""
    decide = _script_function("decide")
    assert "seenFigures(item, intent)" in decide
    seen = _run_js(
        ["seenFigures"],
        'seenFigures({"paid": "80.00", "already_refunded": "15.00"}, "approve")',
    )
    assert seen == {
        "intent": "approve",
        "seen": {"paid": "80.00", "already_refunded": "15.00"},
    }
    assert _run_js(["seenFigures"], 'seenFigures({"paid": "80.00"}, "reject")') == {
        "intent": "reject"
    }


@pytest.mark.edge
def test_a_new_reason_counts_as_a_change_and_is_named() -> None:
    before = {**_SEEN, "reasons": ["Refunds over $25.00 need your approval."]}
    after = {
        **before,
        "reasons": [
            "This order is in a dispute. If you refund it, you may still lose "
            "the dispute.",
            "Refunds over $25.00 need your approval.",
        ],
        "flag": "Open dispute",
    }
    changed = _run_js(
        ["fingerprint"],
        f"fingerprint({json.dumps(before)}) !== fingerprint({json.dumps(after)})",
    )
    assert changed is True
    lines = _run_js(
        ["changedLines", "money"],
        f"changedLines({json.dumps(before)}, {json.dumps(after)})",
    )
    assert lines[0].startswith("This order is in a dispute")


@pytest.mark.edge
def test_a_prompt_refused_for_changed_figures_says_so() -> None:
    shown = {**_SEEN}
    latest = {**_SEEN, "status": "waiting", "already_refunded": "20.00"}
    found = _run_js(
        ["refusedOutcome", "fingerprint"],
        f"refusedOutcome({{}}, {json.dumps(latest)}, {json.dumps(shown)})",
    )
    assert found[1] == "The figures changed. No money moved."


@pytest.mark.edge
@pytest.mark.parametrize(
    ("before", "after", "has_line"),
    [("100.00", "80.00", True), ("65.00", "65.00", False), ("65.00", "", False)],
)
def test_the_changed_screen_only_compares_figures_that_differ(
    before: str, after: str, has_line: bool
) -> None:
    lines = _run_js(
        ["changedLines", "money"],
        f"changedLines({{refundable_now: {json.dumps(before)}}}, "
        f"{{refundable_now: {json.dumps(after)}}})",
    )
    assert any("was" in line for line in lines) is has_line
    assert lines[-1] == "Review the new figures and approve again if it's still right."


@pytest.mark.edge
def test_every_home_render_waits_for_the_soonest_deadline() -> None:
    """Wiring only: the timing itself is tested by running it above."""
    assert "scheduleFlip(data.sections.needs_decision)" in _script_function(
        "renderHome"
    )
    flip = _script_function("scheduleFlip")
    assert "msUntilSoonest(items, serverNow())" in flip
    assert "setTimeout(somethingChanged, wait)" in flip


@pytest.mark.edge
@pytest.mark.parametrize(
    ("minutes", "words"),
    [
        (240, "4 hours"),
        (60, "1 hour"),
        (90, "1 hour 30 minutes"),
        (10, "10 minutes"),
        (1, "1 minute"),
        (1_440, "24 hours"),
    ],
)
def test_the_time_to_decide_reads_in_hours_and_minutes(
    minutes: int, words: str
) -> None:
    found = _run_js(
        ["durationWords"], f"durationWords({minutes})", constants=("MINUTES_PER_HOUR",)
    )
    assert found == words


@pytest.mark.edge
@pytest.mark.parametrize(
    ("outcome", "title"),
    [
        (
            {"state": "denied", "merchant_reason": "There isn't enough left."},
            "Not refunded. No money moved.",
        ),
        (
            {"state": "paused", "merchant_reason": "This customer reached it."},
            "On hold. No money moved.",
        ),
        ({"state": "executed", "paypal_status": "COMPLETED"}, None),
        ({"state": "denied"}, None),  # no reason given: the general wording stays
    ],
)
def test_an_approval_stopped_by_the_fresh_check_says_why(
    outcome: dict[str, str], title: str | None
) -> None:
    found = _run_js(["recheckOutcome"], f"recheckOutcome({json.dumps(outcome)})")
    if title is None:
        assert found is None
        return
    assert found[1] == title
    assert outcome["merchant_reason"] in found[2]


@pytest.mark.edge
@pytest.mark.parametrize(
    ("section", "intent", "title"),
    [
        ("needs_decision", "approve", None),  # still waiting: answer again
        ("sending", "approve", "Your approval reached us"),
        ("sending", "reject", "This refund was already approved"),
        ("failed", "approve", "Your approval reached us"),
        ("cant_send", "approve", "Your answer didn't reach us"),
        ("paused", "reject", "Your answer didn't reach us"),
        (None, "approve", "This request is closed"),  # gone: never "it arrived"
        (None, "reject", "This request is closed"),
    ],
)
def test_a_lost_answer_says_only_what_the_server_shows(
    section: str | None, intent: str, title: str | None
) -> None:
    latest = {"section": section} if section else None
    expression = f"lostAnswerOutcome({json.dumps(latest)}, {json.dumps(intent)})"
    outcome = _run_js(["lostAnswerOutcome"], expression)
    assert (outcome[1] if outcome else None) == title


@pytest.mark.edge
@pytest.mark.parametrize(
    ("status", "name", "text"),
    [
        (403, "", "Fingerprint not recognised. Nothing changed. Try again."),
        (None, "NotAllowedError", "Fingerprint cancelled. Nothing changed."),
        (409, "", "That didn't go through. Nothing changed. Try again."),
    ],
)
def test_a_failed_stop_or_resume_says_what_happened(
    status: int | None, name: str, text: str
) -> None:
    expression = (
        f"controlFailureText({json.dumps(status)}, {{name: {json.dumps(name)}}})"
    )
    assert _run_js(["controlFailureText"], expression) == text


@pytest.mark.edge
@pytest.mark.parametrize(
    ("exact", "noted"),
    [("yes", False), ("no", True), (None, True)],  # a missing answer is not proof
)
def test_an_on_hold_message_not_rebuilt_exactly_says_so(
    exact: str | None, noted: bool
) -> None:
    item = {} if exact is None else {"customer_message_exact": exact}
    note = _run_js(["customerLineNote"], f"customerLineNote({json.dumps(item)})")
    assert bool(note) is noted


@pytest.mark.edge
def test_a_decline_past_the_deadline_is_still_accepted(env: Env) -> None:
    env.propose("r1", "30.00")
    env.enrol()
    session = env.session()
    _gateway(env)._open_seconds = 0
    response, _ = env.decide("r1", "reject", session)
    assert response.status_code == 200
    assert response.json()["state"] == "denied"


# Cost of a home load: many cards on one order, a repeat load, a change in our
# records, done names beyond those shown, and decisions that must stay uncached.
class CountsOrderReads(RecordedPayPalClient):
    def __init__(self, orders: dict[str, Any]) -> None:
        super().__init__(orders)
        self.reads: list[str] = []

    async def get_order(self, order_id: str) -> Any:
        self.reads.append(order_id)
        return await super().get_order(order_id)


@pytest.fixture
def counted(tmp_path: Path) -> Iterator[Env]:
    environment = Env(tmp_path)
    environment.paypal = CountsOrderReads({"ORD-1": order_json(captured="500.00")})
    environment.client = TestClient(
        create_app(environment.settings, environment.paypal)
    )
    with environment.client:
        yield environment


def _reads(env: Env) -> list[str]:
    reads: list[str] = env.paypal.reads  # type: ignore[attr-defined]
    return reads


@pytest.mark.edge
def test_many_cards_on_one_order_cost_one_paypal_read(counted: Env) -> None:
    for n in range(1, 4):
        counted.propose(f"r{n}", "30.00")
    counted.enrol()
    session = counted.session()
    _reads(counted).clear()
    home = _home(counted, session)
    assert _reads(counted) == ["ORD-1"]
    assert sum(len(s) for s in home["sections"].values()) == 3


@pytest.mark.edge
def test_a_repeat_home_load_reuses_reads_until_our_records_change(
    counted: Env,
) -> None:
    counted.propose("r1", "30.00")
    counted.enrol()
    session = counted.session()
    first = _home(counted, session)
    _reads(counted).clear()
    again = _home(counted, session)
    assert _reads(counted) == []
    checked = first["sections"]["needs_decision"][0]["checked_at"]
    assert again["sections"]["needs_decision"][0]["checked_at"] == checked
    counted.propose("r2", "30.00")  # a change in our records
    _reads(counted).clear()  # the proposal read PayPal for its own decision
    _home(counted, session)
    assert _reads(counted) == ["ORD-1"]


@pytest.mark.edge
def test_home_reads_names_only_for_the_done_requests_it_shows(
    counted: Env, monkeypatch: pytest.MonkeyPatch
) -> None:
    counted.enrol()
    session = counted.session()
    ended = [
        {
            "request_id": f"d{n}",
            "tool": "create_refund",
            "amount": "5.00",
            "currency": "USD",
            "order_id": f"ORD-D{n}",
            "state": "denied",
            "updated_at": datetime.now(UTC).isoformat(),
        }
        for n in range(8)
    ]
    gateway = _gateway(counted)
    monkeypatch.setattr(gateway._ledger, "recent_outcomes", lambda now: ended)
    _reads(counted).clear()
    home = _home(counted, session)
    assert len(home["done"]["recent"]) == 3
    assert sorted(_reads(counted)) == ["ORD-D0", "ORD-D1", "ORD-D2"]


@pytest.mark.edge
def test_a_decision_reads_paypal_again_even_right_after_a_home_load(
    counted: Env,
) -> None:
    counted.propose("r1", "30.00")
    counted.enrol()
    session = counted.session()
    _home(counted, session)
    _reads(counted).clear()
    response, _ = counted.decide("r1", "approve", session)
    assert response.json()["state"] == "executed"
    assert "ORD-1" in _reads(counted)  # the refund used fresh facts, not the screen's


@pytest.mark.edge
def test_home_explains_money_held_by_other_requests(env: Env) -> None:
    env.propose("r1", "10.00")
    env.propose("r2", "20.00")
    env.enrol()
    home = _home(env, env.session())
    r2 = next(i for i in home["sections"]["needs_decision"] if i["request_id"] == "r2")
    assert r2["waiting_note"] == "$10.00 waiting on another request"
    r1 = next(i for i in home["sections"]["needs_decision"] if i["request_id"] == "r1")
    assert r1["waiting_note"] == "$20.00 waiting on another request"
    assert home["today"]["waiting_total"] == "30.00"
    assert home["today"]["limit_used"] == "30.00"


@pytest.mark.edge
def test_home_shows_only_the_latest_three_done(env: Env) -> None:
    env.enrol()
    session = env.session()
    # one at a time, so declined requests never count towards a repeat pause
    for n in range(5):
        env.propose(f"r{n}", "1.00")
        env.decide(f"r{n}", "reject", session)
    home = _home(env, session)
    assert len(home["done"]["recent"]) == 3
    assert home["done"]["total"] == "5"


@pytest.mark.edge
def test_home_shows_the_name_calmly_without_changing_what_was_stored(
    env: Env,
) -> None:
    env.enrol("VIKAS DAYASHANKAR SAHANI")
    session = env.session()
    home = _home(env, session)
    assert home["approver_display"] == "Vikas Dayashankar Sahani"
    history_name = env.client.post("/v1/approver/session/options").status_code
    assert history_name == 200


@pytest.mark.edge
def test_the_live_stream_needs_a_signed_in_phone(env: Env) -> None:
    assert env.client.get("/v1/approvals/stream").status_code == 401


@pytest.mark.edge
def test_the_live_stream_reads_the_database_off_the_event_loop(
    env: Env, monkeypatch: pytest.MonkeyPatch
) -> None:
    env.enrol()
    session = env.session()
    gateway = _gateway(env)
    on_loop: list[bool] = []
    real_version, real_session = gateway.live_version, gateway.session_is_valid

    def note_where_called() -> None:
        try:
            asyncio.get_running_loop()
            on_loop.append(True)
        except RuntimeError:
            on_loop.append(False)

    def version() -> int:
        note_where_called()
        return real_version()

    checks: list[str] = []

    def session_ok(token: str) -> bool:
        checks.append(token)
        if len(checks) == 1:  # the sign-in check before the stream opens
            return real_session(token)
        note_where_called()
        return False  # the session ends, so the stream closes as it would live

    monkeypatch.setattr(gateway, "live_version", version)
    monkeypatch.setattr(gateway, "session_is_valid", session_ok)
    with env.client.stream("GET", "/v1/approvals/stream", headers=session) as live:
        lines = [line for line in live.iter_lines() if line.startswith("event:")]
    assert lines == ["event: changed", "event: signed_out"]
    assert on_loop == [False, False]  # the version read and the session check


def test_every_configured_stream_path_is_a_real_streaming_route(env: Env) -> None:
    for path in env.settings.limits.streaming_paths:
        # a real route asks for a signed-in phone; a path typed wrong is a 404
        assert env.client.get(path).status_code == 401


def test_the_merchant_page_never_claims_a_check_before_one_happens(env: Env) -> None:
    page = env.client.get("/approve").text
    assert "Checked just now" not in page
    style = env.client.get("/assets/approve.css").text
    assert "overscroll-behavior-y: contain" in style  # a pull never reloads


def test_the_merchant_script_offers_send_only_when_the_server_says_yes() -> None:
    script = (
        Path(__file__).parents[2] / "agentcosign" / "web" / "approve.js"
    ).read_text(encoding="utf-8")
    assert 'can_send === "yes"' in script
    assert "item.fits" not in script  # the phone never decides by itself


@pytest.mark.edge
def test_the_page_always_loads_the_script_and_style_it_was_built_with(
    env: Env,
) -> None:
    import hashlib
    import re

    web = Path(__file__).parents[2] / "agentcosign" / "web"
    page = env.client.get("/approve").text
    for name in ("approve.js", "approve.css"):
        digest = hashlib.sha256((web / name).read_bytes()).hexdigest()[:12]
        path = f"/static/{digest}/{name}"
        assert path in page
        asset = env.client.get(path)
        assert asset.status_code == 200
        assert asset.headers["cache-control"] == "public, max-age=31536000, immutable"
        # an old or made-up version is never answered with today's file
        assert env.client.get(f"/static/000000000000/{name}").status_code == 404
    assert not re.search(r'"/assets/', page)  # nothing linked without its version


@pytest.mark.edge
def test_unversioned_assets_are_always_revalidated(env: Env) -> None:
    response = env.client.get("/assets/approve.js")
    assert response.headers["cache-control"] == "no-cache"


@pytest.mark.edge
def test_refund_data_is_never_stored_in_a_browser_cache(env: Env) -> None:
    env.propose("r1", "10.00")
    env.enrol()
    session = env.session()
    for path in (
        "/v1/approvals/home",
        "/v1/approvals/done",
        "/v1/approvals/r1/history",
    ):
        assert env.client.get(path, headers=session).headers["cache-control"] == (
            "no-store"
        )


@pytest.mark.edge
def test_an_open_tab_can_tell_when_the_server_was_updated(env: Env) -> None:
    import re

    env.enrol()
    page = env.client.get("/approve").text
    found = re.search(r'<meta name="build" content="([0-9a-f]{12})">', page)
    assert found
    assert _home(env, env.session())["build"] == found.group(1)
    check = _script_function("checkBuild")
    assert "removeSendButtons" in check  # a stale tab never sends


@pytest.mark.edge
def test_the_final_check_figures_add_up_when_other_requests_hold_money() -> None:
    final = _script_function("openFinal")
    # paid, refunded, held by others and left after must all be on the last screen
    assert "Held for other requests" in final and "waiting_on_order" in final


@pytest.mark.edge
def test_money_set_aside_is_not_called_waiting_for_a_decision() -> None:
    today = _script_function("renderSummary")
    assert "held for open requests" in today
    assert "waiting for your decision" not in today  # paused money cannot be decided


@pytest.mark.edge
async def test_the_record_never_names_a_device_it_cannot_know(env: Env) -> None:
    env.propose("r1", "10.00")
    env.enrol()
    session = env.session()
    env.decide("r1", "approve", session)
    record = env.client.get("/v1/approvals/r1/history", headers=session).json()
    details = [e["detail"] for e in record["events"]]
    assert "Confirmed with fingerprint or screen lock" in details
    assert not any("phone" in d for d in details)


@pytest.mark.edge
async def test_a_refund_paypal_is_still_processing_is_never_listed_as_sent(
    tmp_path: Path,
) -> None:
    env = Env(tmp_path)
    env.paypal = RecordedPayPalClient(
        {"ORD-1": order_json(captured="500.00")}, refund_status="PENDING"
    )
    env.client = TestClient(create_app(env.settings, env.paypal))
    with env.client:
        env.propose("r1", "10.00")
        env.enrol()
        session = env.session()
        env.decide("r1", "approve", session)
        done = _home(env, session)["done"]["recent"]
        assert done[0]["paypal_status"] == "PENDING"
        env.paypal.set_refund_status("r1", "COMPLETED")
        await _gateway(env).settle_pending()
        done = _home(env, session)["done"]["recent"]
        assert done[0]["paypal_status"] == "COMPLETED"


@pytest.mark.edge
async def test_record_steps_let_the_phone_track_a_send_without_reading_words(
    env: Env,
) -> None:
    env.propose("r1", "10.00")
    env.propose("r2", "12.00")
    env.enrol()
    session = env.session()
    env.decide("r1", "approve", session)
    env.decide("r2", "reject", session)
    sent = env.client.get("/v1/approvals/r1/history", headers=session).json()
    assert [e["step"] for e in sent["events"]] == [
        "asked",
        "routed",
        "approved",
        "rechecked",
        "paypal_completed",
    ]
    declined = env.client.get("/v1/approvals/r2/history", headers=session).json()
    assert [e["step"] for e in declined["events"]] == ["asked", "routed", "declined"]


def test_the_send_tracker_ticks_only_from_recorded_steps() -> None:
    tracker = _script_function("refreshTracker")
    assert "/history" in tracker and "event.step" in tracker
    reveal = _script_function("revealSteps")
    # only steps the server recorded are ticked, in order, one at a time
    assert "recorded.indexOf(step) !== -1" in reveal
    notices = _script_function("noticesFor")
    # a completed notice is only shown when PayPal confirmed it
    assert 'item.paypal_status === "COMPLETED"' in notices


@pytest.mark.edge
def test_bursts_of_changes_never_flood_paypal_with_reads() -> None:
    read = _script_function("readHome")
    assert "if (reading)" in read and "readAgain = true" in read


# --- the emergency stop ------------------------------------------------------------
def _control(env: Env, intent: str, session: dict[str, str]) -> Any:
    options = env.client.post(f"/v1/controls/{intent}/options", headers=session)
    assert options.status_code == 200, options.text
    return env.client.post(
        f"/v1/controls/{intent}",
        json={"credential": env.phone.sign(options.text)},
        headers=session,
    )


@pytest.mark.edge
def test_a_stop_refuses_new_requests_and_tells_the_customer_kindly(env: Env) -> None:
    env.enrol()
    session = env.session()
    assert _control(env, "stop", session).json()["refunds_stopped"] == "yes"
    outcome = env.propose("r1", "5.00").json()
    assert outcome["decision"] == "deny" and outcome["rule_id"] == "R16"
    status = env.client.get("/v1/proposals/r1", headers=env.key("agent_api_key"))
    message = status.json()["customer_message"]
    assert "can't make refunds right now" in message and "reply here" in message.lower()


@pytest.mark.edge
def test_while_stopped_waiting_requests_cannot_be_approved_but_can_be_declined(
    env: Env,
) -> None:
    env.propose("r1", "30.00")
    env.propose("r2", "31.00")
    env.enrol()
    session = env.session()
    _control(env, "stop", session)
    home = _home(env, session)
    assert home["refunds_stopped"] == "yes"
    item = next(i for i in home["sections"]["cant_send"] if i["request_id"] == "r1")
    assert item["can_send"] == "no" and item["cant_send_reason"] == "stopped"
    refused = env.client.post(
        "/v1/approvals/r1/options",
        json={"intent": "approve", "seen": env.seen()},
        headers=session,
    )
    assert refused.status_code == 409
    declined, _ = env.decide("r2", "reject", session)
    assert declined.status_code == 200


@pytest.mark.edge
async def test_on_hold_requests_stay_on_hold_while_refunds_are_stopped(
    env: Env,
) -> None:
    _three_then_a_fourth(env)
    env.enrol()
    session = env.session()
    _control(env, "stop", session)
    gateway = _gateway(env)
    later = datetime.now(UTC) + timedelta(hours=30)
    gateway._clock = lambda: later
    assert await gateway.release_due() == []
    assert (
        env.client.get("/v1/proposals/r4", headers=env.key("agent_api_key")).json()[
            "state"
        ]
        == "paused"
    )


@pytest.mark.edge
def test_a_stop_signature_can_never_turn_refunds_back_on(env: Env) -> None:
    env.enrol()
    session = env.session()
    stop_options = env.client.post("/v1/controls/stop/options", headers=session).text
    signed = env.phone.sign(stop_options)
    env.client.post("/v1/controls/stop", json={"credential": signed}, headers=session)
    replayed = env.client.post(
        "/v1/controls/resume", json={"credential": signed}, headers=session
    )
    assert replayed.status_code == 403  # the signature is not for resuming
    assert _home(env, session)["refunds_stopped"] == "yes"


def test_resuming_with_a_fresh_fingerprint_turns_refunds_back_on(env: Env) -> None:
    env.enrol()
    session = env.session()
    _control(env, "stop", session)
    resumed = _control(env, "resume", session).json()
    assert resumed["refunds_stopped"] == "no" and resumed["changed"] == "yes"
    assert env.propose("r1", "30.00").json()["decision"] == "escalate"


@pytest.mark.edge
def test_unknown_control_intents_are_refused_at_the_edge(env: Env) -> None:
    env.enrol()
    session = env.session()
    assert env.client.post("/v1/controls/pay/options", headers=session).status_code == (
        422
    )


@pytest.mark.edge
def test_the_operator_can_stop_refunds_but_not_turn_them_back_on(
    env: Env, monkeypatch: pytest.MonkeyPatch
) -> None:
    from agentcosign import cli

    assert cli.stop_refunds(env.settings) == 1  # never with a key the server lacks
    configured = _operator_settings(env, monkeypatch)
    assert "audit_hmac_key" not in configured.ephemeral_secrets
    assert cli.stop_refunds(configured) == 0
    env.enrol()
    assert _home(env, env.session())["refunds_stopped"] == "yes"
    assert not hasattr(cli, "resume_refunds")


def test_the_stop_controls_follow_the_server_state() -> None:
    render = _script_function("renderStop")
    assert "Turn refunds back on" in render and "Stop all refunds" in render
    home = _script_function("renderHome")
    assert 'data.refunds_stopped === "yes"' in home  # the server decides, not the phone


# --- revoking a lost phone -----------------------------------------------------------
def _operator_settings(env: Env, monkeypatch: pytest.MonkeyPatch) -> Settings:
    """Settings for an operator configured like the server: the same audit key."""
    monkeypatch.setenv(
        "AGENTCOSIGN_AUDIT_KEY", env.settings.audit_hmac_key.get_secret_value()
    )
    get_settings.cache_clear()
    try:
        return get_settings().model_copy(
            update={
                "sqlite_db_path": env.settings.sqlite_db_path,
                "paypal_merchant_id": env.settings.paypal_merchant_id,
                "public_origin": env.settings.public_origin,
            }
        )
    finally:
        get_settings.cache_clear()  # later tests load their own settings


@pytest.mark.edge
def test_revoking_a_phone_ends_its_open_session_and_is_recorded(
    env: Env, monkeypatch: pytest.MonkeyPatch
) -> None:
    from agentcosign import cli

    env.enrol()
    session = env.session()
    assert _home(env, session)["counts"]
    assert cli.revoke_approver(_operator_settings(env, monkeypatch), "Vikas") == 0
    assert env.client.get("/v1/approvals/home", headers=session).status_code == 401
    ledger = _gateway(env)._ledger
    assert [e["event"] for e in ledger.events("control:passkeys")] == [
        "passkey_revoked"
    ]
    ledger.verify_records()


@pytest.mark.edge
def test_a_decision_signed_by_a_revoked_phone_is_refused(
    env: Env, monkeypatch: pytest.MonkeyPatch
) -> None:
    from agentcosign import cli

    env.propose("r1", "30.00")
    env.enrol()
    lost = SoftAuthenticator("localhost", ORIGIN, credential_id=b"cred-lost")
    operator = env.key("operator_api_key")
    options = env.client.post(
        "/v1/approver/enrolment/options", json={"approver": "Lost"}, headers=operator
    ).text
    env.client.post(
        "/v1/approver/enrolment",
        json={"approver": "Lost", "credential": lost.register(options)},
        headers=operator,
    )
    session = env.session()  # opened with the phone that is kept
    assert cli.revoke_approver(_operator_settings(env, monkeypatch), "Lost") == 0
    options = env.client.post(
        "/v1/approvals/r1/options",
        json={"intent": "approve", "seen": env.seen()},
        headers=session,
    ).text
    refused = env.client.post(
        "/v1/approvals/r1/approve",
        json={"credential": lost.sign(options)},
        headers=session,
    )
    assert refused.status_code == 403  # a revoked passkey's signature is refused
    assert _home(env, session)["sections"]["needs_decision"][0]["request_id"] == "r1"


@pytest.mark.edge
def test_revoking_an_unknown_name_says_so_and_changes_nothing(
    env: Env, monkeypatch: pytest.MonkeyPatch
) -> None:
    from agentcosign import cli

    env.enrol()
    assert cli.revoke_approver(_operator_settings(env, monkeypatch), "Nobody") == 1
    assert env.session()


@pytest.mark.edge
def test_the_published_head_shows_no_money_details(env: Env) -> None:
    env.propose("r1", "10.00")
    head = env.client.get("/api/v1/audit/head")
    assert head.status_code == 200
    assert set(head.json()) == {"seq", "entry_hash", "created_at"}
    assert head.headers["cache-control"] == "no-store"


def test_the_operator_can_print_and_check_the_chain_head(
    env: Env, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    from agentcosign import cli

    env.propose("r1", "10.00")
    settings = _operator_settings(env, monkeypatch)
    assert cli.audit_head(settings) == 0
    printed = capsys.readouterr().out
    seq = re.search(r"seq=(\d+)", printed)
    digest = re.search(r"hash=([0-9a-f]{64})", printed)
    assert seq and digest
    assert cli.verify_head(settings, int(seq.group(1)), digest.group(1)) == 0
    assert cli.verify_head(settings, int(seq.group(1)), "f" * 64) == 1


# --- reconciliation with PayPal ----------------------------------------------------
def _refunds_at_paypal(env: Env) -> list[dict[str, Any]]:
    payments = env.paypal._orders["ORD-1"]["purchase_units"][0]["payments"]
    refunds: list[dict[str, Any]] = payments.setdefault("refunds", [])
    return refunds


async def test_a_sent_refund_reconciles_clean_and_the_run_is_recorded(
    env: Env,
) -> None:
    env.propose("r1", "30.00")
    env.enrol()
    session = env.session()
    env.decide("r1", "approve", session)
    report = await _gateway(env).reconcile()
    assert report.matched == 1 and not report.breaks and not report.unchecked
    events = _gateway(env)._ledger.events("control:reconciliation")
    assert [e["event"] for e in events] == ["reconciled"]
    run = _home(env, session)["reconciliation"]
    assert run["breaks"] == "0" and run["lookback_days"] == "7"


@pytest.mark.edge
async def test_a_refund_missing_at_paypal_is_a_break_the_merchant_sees(
    env: Env,
) -> None:
    env.propose("r1", "30.00")
    env.enrol()
    session = env.session()
    env.decide("r1", "approve", session)
    _refunds_at_paypal(env).clear()  # PayPal shows none of the refunds we sent
    report = await _gateway(env).reconcile()
    assert [(b.request_id, b.kind) for b in report.breaks] == [("r1", "missing")]
    assert _home(env, session)["reconciliation"]["breaks"] == "1"


@pytest.mark.edge
async def test_an_order_paypal_will_not_show_is_reported_unchecked(env: Env) -> None:
    env.propose("r1", "30.00")
    env.paypal._orders.pop("ORD-1")
    report = await _gateway(env).reconcile()
    assert report.unchecked == ("ORD-1",) and report.matched == 0


_BACKDATE_ASKED = "UPDATE proposals SET created_at = ? WHERE request_id = ?"
_BACKDATE_ALL = (
    "UPDATE proposals SET created_at = ?, updated_at = ? WHERE request_id = ?"
)
_MARK_FAILED = "UPDATE proposals SET state = 'failed' WHERE request_id = ?"


def _sent(env: Env, *request_ids: str) -> None:
    env.enrol()
    session = env.session()
    for request_id in request_ids:
        env.propose(request_id, "30.00")
        response, _ = env.decide(request_id, "approve", session)
        assert response.json()["state"] == "executed"


def _db(env: Env, sql: str, *params: str) -> None:
    with sqlite3.connect(env.settings.sqlite_db_path) as conn:
        conn.execute(sql, params)


@pytest.mark.edge
async def test_a_request_asked_before_the_window_but_sent_inside_is_reconciled(
    env: Env,
) -> None:
    _sent(env, "r1")
    _db(env, _BACKDATE_ASKED, (datetime.now(UTC) - timedelta(days=8)).isoformat(), "r1")
    report = await _gateway(env).reconcile()
    assert report.matched == 1 and not report.unchecked


@pytest.mark.edge
async def test_our_refund_sent_before_the_window_is_not_reported_as_outside(
    env: Env,
) -> None:
    _sent(env, "r1", "r2")
    old = (datetime.now(UTC) - timedelta(days=8)).isoformat()
    _db(env, _BACKDATE_ALL, old, old, "r1")
    report = await _gateway(env).reconcile()
    assert report.matched == 1 and not report.breaks
    assert report.outside == ()


@pytest.mark.edge
async def test_money_that_moved_for_a_request_we_marked_failed_is_a_break(
    env: Env,
) -> None:
    _sent(env, "r1", "r2")
    _db(env, _MARK_FAILED, "r1")
    report = await _gateway(env).reconcile()
    assert [(b.request_id, b.kind) for b in report.breaks] == [
        ("r1", "sent_without_our_record")
    ]
    assert report.outside == () and report.matched == 1


@pytest.mark.edge
async def test_reconciliation_runs_again_only_when_it_is_due(env: Env) -> None:
    gateway = _gateway(env)
    assert await gateway.reconcile_if_due() is not None  # never run before
    assert await gateway.reconcile_if_due() is None  # ran moments ago
    later = datetime.now(UTC) + timedelta(hours=25)
    gateway._clock = lambda: later
    assert await gateway.reconcile_if_due() is not None


def test_the_operator_can_run_reconciliation_and_read_the_report(
    env: Env, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    from agentcosign import cli

    env.propose("r1", "30.00")
    settings = _operator_settings(env, monkeypatch)
    assert cli.reconcile(settings, env.paypal) == 0
    printed = capsys.readouterr().out
    assert "matched" in printed and "breaks" in printed


def test_reconciliation_notices_come_from_the_server_record() -> None:
    notices = _script_function("reconciliationNotices")
    assert "run.breaks" in notices and "never moves money" in notices


# --- nothing the merchant reads is typed in twice ---------------------------------
def test_the_sign_out_message_uses_the_configured_session_length(env: Env) -> None:
    env.enrol()
    options = env.client.post("/v1/approver/session/options").text
    body = env.client.post(
        "/v1/approver/session", json={"credential": env.phone.sign(options)}
    ).json()
    assert body["session_minutes"] == str(env.settings.approval.session_seconds // 60)
    assert "10 minutes" not in _script_function("signedOut")


def test_the_window_comes_from_the_server_not_the_phone(env: Env) -> None:
    env.enrol()
    assert _home(env, env.session())["settings"]["window_hours"] == "24"
    script = (
        Path(__file__).parents[2] / "agentcosign" / "web" / "approve.js"
    ).read_text(encoding="utf-8")
    assert "24 hours" not in script


@pytest.mark.edge
def test_a_capped_activity_list_never_hides_the_true_total(
    env: Env, monkeypatch: pytest.MonkeyPatch
) -> None:
    from agentcosign.services import ledger as ledger_module

    monkeypatch.setattr(ledger_module, "RECENT_LIMIT", 3)
    env.enrol()
    session = env.session()
    for n in range(5):
        env.propose(f"r{n}", "1.00")
        env.decide(f"r{n}", "reject", session)
    page = env.client.get("/v1/approvals/done", headers=session).json()
    assert len(page["items"]) == 3 and page["total"] == "5"
    assert _home(env, session)["done"]["total"] == "5"


def test_the_setup_link_message_follows_the_configured_lifetime(
    env: Env, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    from agentcosign import cli

    settings = _operator_settings(env, monkeypatch)
    shorter = settings.model_copy(
        update={
            "approval": settings.approval.model_copy(
                update={"enrolment_link_seconds": 300}
            )
        }
    )
    assert cli.enrol_link(shorter) == 0
    assert "within 5 minutes" in capsys.readouterr().out


# --- verified PayPal webhooks --------------------------------------------------------
SIGNED = {
    "PAYPAL-TRANSMISSION-ID": "trans-1",
    "PAYPAL-TRANSMISSION-TIME": "2026-10-13T00:00:00Z",
    "PAYPAL-TRANSMISSION-SIG": "signature",
    "PAYPAL-CERT-URL": "https://api.sandbox.paypal.com/v1/notifications/certs/c",
    "PAYPAL-AUTH-ALGO": "SHA256withRSA",
}


def _event(event_id: str = "WH-1", kind: str = "PAYMENT.CAPTURE.REFUNDED") -> Any:
    return {"id": event_id, "event_type": kind, "resource": {"id": "REF-1"}}


def _with_webhooks(env: Env, webhook_id: str = "WEBHOOK-1") -> Env:
    """Live-sandbox mode with a webhook id, as a real deployment would run."""
    env.settings = env.settings.model_copy(
        update={"paypal_webhook_id": webhook_id, "paypal_execution": "sandbox"}
    )
    env.client = TestClient(create_app(env.settings, env.paypal))
    env.paypal.set_webhook_verification("SUCCESS")  # PayPal says the event is genuine
    return env


def _webhook_events(env: Env) -> list[str]:
    ledger = _gateway(env)._ledger
    return [e["event_id"] for e in ledger.events("control:webhooks")]


@pytest.mark.edge
def test_a_verified_webhook_is_accepted_and_recorded(tmp_path: Path) -> None:
    env = _with_webhooks(Env(tmp_path))
    with env.client:
        answer = env.client.post("/v1/webhooks/paypal", json=_event(), headers=SIGNED)
        assert answer.status_code == 200 and answer.json()["status"] == "accepted"
        assert _webhook_events(env) == ["WH-1"]


@pytest.mark.edge
def test_a_webhook_paypal_does_not_verify_is_refused_and_not_recorded(
    tmp_path: Path,
) -> None:
    env = _with_webhooks(Env(tmp_path))
    env.paypal.set_webhook_verification("FAILURE")
    with env.client:
        answer = env.client.post("/v1/webhooks/paypal", json=_event(), headers=SIGNED)
        assert answer.status_code == 400
        assert _webhook_events(env) == []


@pytest.mark.edge
def test_when_paypal_cannot_verify_the_webhook_it_is_asked_to_retry(
    tmp_path: Path,
) -> None:
    env = _with_webhooks(Env(tmp_path))
    env.paypal.set_webhook_verification(None)  # PayPal unreachable
    with env.client:
        answer = env.client.post("/v1/webhooks/paypal", json=_event(), headers=SIGNED)
        assert answer.status_code == 503
        assert _webhook_events(env) == []


@pytest.mark.edge
def test_a_webhook_without_signature_headers_is_refused(tmp_path: Path) -> None:
    env = _with_webhooks(Env(tmp_path))
    with env.client:
        unsigned = {k: v for k, v in SIGNED.items() if k != "PAYPAL-TRANSMISSION-SIG"}
        answer = env.client.post("/v1/webhooks/paypal", json=_event(), headers=unsigned)
        assert answer.status_code == 400


@pytest.mark.edge
def test_an_unknown_event_type_is_acknowledged_and_ignored(tmp_path: Path) -> None:
    env = _with_webhooks(Env(tmp_path))
    with env.client:
        answer = env.client.post(
            "/v1/webhooks/paypal",
            json=_event(kind="BILLING.PLAN.CREATED"),
            headers=SIGNED,
        )
        assert answer.status_code == 200 and answer.json()["status"] == "ignored"
        assert _webhook_events(env) == []


@pytest.mark.edge
def test_the_same_event_delivered_twice_is_handled_once(tmp_path: Path) -> None:
    env = _with_webhooks(Env(tmp_path))
    with env.client:
        env.client.post("/v1/webhooks/paypal", json=_event(), headers=SIGNED)
        again = env.client.post("/v1/webhooks/paypal", json=_event(), headers=SIGNED)
        assert again.status_code == 200 and again.json()["status"] == "duplicate"
        assert _webhook_events(env) == ["WH-1"]


@pytest.mark.edge
def test_webhooks_are_refused_until_a_webhook_id_is_configured(env: Env) -> None:
    answer = env.client.post("/v1/webhooks/paypal", json=_event(), headers=SIGNED)
    assert answer.status_code == 503


@pytest.mark.edge
def test_an_oversized_webhook_body_is_refused(tmp_path: Path) -> None:
    env = _with_webhooks(Env(tmp_path))
    with env.client:
        huge = {**_event(), "padding": "x" * 70_000}
        answer = env.client.post("/v1/webhooks/paypal", json=huge, headers=SIGNED)
        assert answer.status_code == 413


@pytest.mark.edge
def test_the_hermetic_demo_never_accepts_a_webhook_even_with_an_id_set(
    tmp_path: Path,
) -> None:
    env = Env(tmp_path)
    # recorded PayPal, as on the hosted demo, with a webhook id set by mistake
    env.settings = env.settings.model_copy(
        update={"paypal_webhook_id": "WEBHOOK-1", "paypal_execution": "recorded"}
    )
    env.client = TestClient(create_app(env.settings, env.paypal))
    env.paypal.set_webhook_verification("SUCCESS")  # even a simulator saying yes
    with env.client:
        answer = env.client.post("/v1/webhooks/paypal", json=_event(), headers=SIGNED)
        assert answer.status_code == 503
        assert _webhook_events(env) == []


# --- the stop holds wherever money could leave --------------------------------------
@pytest.mark.edge
def test_an_approval_signed_before_the_stop_cannot_send_after_it(env: Env) -> None:
    env.propose("r1", "30.00")
    env.enrol()
    session = env.session()
    options = env.client.post(
        "/v1/approvals/r1/options",
        json={"intent": "approve", "seen": env.seen()},
        headers=session,
    ).text
    signed = env.phone.sign(options)  # fingerprint given before the stop
    _gateway(env)._ledger.set_refunds_stopped(True, "Vikas", datetime.now(UTC))
    refused = env.client.post(
        "/v1/approvals/r1/approve", json={"credential": signed}, headers=session
    )
    assert refused.status_code == 409
    status = env.client.get("/v1/proposals/r1", headers=env.key("agent_api_key"))
    assert status.json()["state"] == "pending_approval"


@pytest.mark.edge
async def test_a_stuck_send_is_not_sent_while_stopped_and_finishes_once_after(
    env: Env,
) -> None:
    env.propose("r1", "30.00")
    env.enrol()
    session = env.session()
    # PayPal answers the check at approval, then cannot be read at send time
    reads = iter(
        [True, True, True]
    )  # screen figures, before the fingerprint, at approval
    real_get = env.paypal.get_order

    async def flaky(order_id: str) -> Any:
        if next(reads, False):
            return await real_get(order_id)
        raise PayPalClientError("unreachable")

    env.paypal.get_order = flaky  # type: ignore[method-assign]
    env.decide("r1", "approve", session)
    env.paypal.get_order = real_get  # type: ignore[method-assign]
    gateway = _gateway(env)
    gateway._ledger.set_refunds_stopped(True, "Vikas", datetime.now(UTC))
    later = datetime.now(UTC) + timedelta(seconds=RETRY_QUIET_SECONDS + 5)
    gateway._clock = lambda: later
    await gateway.finish_unfinished()
    assert _refunds_at_paypal(env) == []  # nothing new left while stopped
    gateway._ledger.set_refunds_stopped(False, "Vikas", later)
    gateway._clock = lambda: later + timedelta(seconds=RETRY_QUIET_SECONDS + 5)
    await gateway.finish_unfinished()
    assert len(_refunds_at_paypal(env)) == 1  # sent once, after refunds resumed


@pytest.mark.edge
async def test_a_stop_pressed_during_release_keeps_the_request_on_hold(
    env: Env,
) -> None:
    _three_then_a_fourth(env)
    gateway = _gateway(env)
    gateway._ledger.set_refunds_stopped(True, "Vikas", datetime.now(UTC))
    evidence = await gateway._evidence(gateway._ledger.load("r4")[0], gateway._clock())
    with pytest.raises(FailClosedError):
        gateway._ledger.release(
            "r4",
            evidence,
            Signals(screening=ScreeningVerdict.UNAVAILABLE),
            gateway._screening,
            datetime.now(UTC) + timedelta(hours=30),
        )
    status = env.client.get("/v1/proposals/r4", headers=env.key("agent_api_key"))
    assert status.json()["state"] == "paused"


@pytest.mark.edge
async def test_a_refund_paypal_is_still_processing_is_not_counted_as_refunded(
    tmp_path: Path,
) -> None:
    env = Env(tmp_path)
    env.paypal = RecordedPayPalClient(
        {"ORD-1": order_json(captured="500.00")}, refund_status="PENDING"
    )
    env.client = TestClient(create_app(env.settings, env.paypal))
    with env.client:
        env.propose("r1", "30.00")
        env.enrol()
        session = env.session()
        env.decide("r1", "approve", session)
        today = _home(env, session)["today"]
        assert today["sent_total"] == "0.00" and today["sent_count"] == "0"
        assert today["processing_count"] == "1"
        assert today["processing_total"] == "30.00"


def test_a_lost_answer_on_stop_or_resume_shows_the_real_state() -> None:
    control = _script_function("control")
    # once the fingerprint left the phone, never claim nothing changed
    assert "signedAndSent" in control and "readHome" in control
