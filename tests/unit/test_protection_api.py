"""Protection middleware and routes, driven through the real FastAPI stack."""

from collections.abc import Iterator

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from agentcosign.api.protection import ProtectionMiddleware
from agentcosign.api.traffic import router
from agentcosign.core.config import LimitSettings, get_settings
from agentcosign.core.limits import TrafficGuard
from agentcosign.main import app as real_app
from tests.unit.test_limits import make_limits

REVIEWER_TOKEN = "j" * 40


class Clock:
    def __init__(self) -> None:
        self.now = 1_000_000

    def __call__(self) -> int:
        return self.now


def build_app(
    limits: LimitSettings, clock: Clock, token: str = REVIEWER_TOKEN
) -> tuple[FastAPI, TrafficGuard]:
    app = FastAPI()
    guard = TrafficGuard(limits)
    settings = get_settings().model_copy(
        update={"limits": limits, "reviewer_token": _secret(token)}
    )
    app.state.guard = guard
    app.state.settings = settings
    app.add_middleware(
        ProtectionMiddleware,
        guard=guard,
        limits=limits,
        reviewer_token=token,
        clock=clock,
    )
    app.include_router(router)

    @app.get("/ping")
    async def ping() -> dict[str, bool]:
        """Cheap route used to exercise the limiter."""
        return {"ok": True}

    @app.post("/echo")
    async def echo(request_body: dict[str, str]) -> dict[str, int]:
        """Accept a small JSON body."""
        return {"keys": len(request_body)}

    @app.get("/health")
    async def health() -> dict[str, str]:
        """Exempt liveness route."""
        return {"status": "healthy"}

    return app, guard


def _secret(value: str):  # noqa: ANN202
    from pydantic import SecretStr

    return SecretStr(value)


@pytest.fixture
def clock() -> Clock:
    return Clock()


@pytest.fixture
def client(clock: Clock) -> Iterator[TestClient]:
    app, _ = build_app(make_limits(), clock)
    with TestClient(app) as test_client:
        yield test_client


def test_burst_is_limited_and_the_refusal_is_honest_json(client: TestClient) -> None:
    statuses = [client.get("/ping").status_code for _ in range(4)]
    assert statuses == [200, 200, 200, 429]
    refused = client.get("/ping")
    assert refused.status_code == 429
    assert refused.headers["retry-after"].isdigit()
    body = refused.json()
    assert body["error"] in {"rate_limited", "blocked"}
    assert body["retry_after_seconds"] >= 1
    assert "—" not in body["message"]


def test_health_stays_reachable_while_the_client_is_limited(
    client: TestClient,
) -> None:
    for _ in range(10):
        client.get("/ping")
    assert client.get("/health").status_code == 200


def test_bucket_recovers_when_time_passes(client: TestClient, clock: Clock) -> None:
    for _ in range(4):
        client.get("/ping")
    clock.now += 11_000
    assert client.get("/ping").status_code == 200


def test_oversized_declared_body_is_refused_before_the_route(
    client: TestClient,
) -> None:
    response = client.post("/echo", content=b"x" * 70_000)
    assert response.status_code == 413
    assert response.json()["error"] == "too_large"


def test_spoofed_forwarded_header_cannot_dodge_the_limit(client: TestClient) -> None:
    # The proxy appends the real client last; spoofed leading entries are ignored.
    for index in range(3):
        headers = {"x-forwarded-for": f"9.9.9.{index}, 203.0.113.7"}
        assert client.get("/ping", headers=headers).status_code == 200
    spoofed = {"x-forwarded-for": "1.2.3.4, 203.0.113.7"}
    assert client.get("/ping", headers=spoofed).status_code == 429


def test_different_real_clients_do_not_share_a_bucket(client: TestClient) -> None:
    for _ in range(4):
        client.get("/ping", headers={"x-forwarded-for": "203.0.113.7"})
    other = client.get("/ping", headers={"x-forwarded-for": "203.0.113.8"})
    assert other.status_code == 200


def test_reviewer_header_raises_the_ceiling(client: TestClient) -> None:
    headers = {"x-reviewer-token": REVIEWER_TOKEN}
    statuses = [client.get("/ping", headers=headers).status_code for _ in range(8)]
    assert set(statuses) == {200}


def test_wrong_reviewer_token_gets_public_limits(client: TestClient) -> None:
    headers = {"x-reviewer-token": "wrong" * 10}
    statuses = [client.get("/ping", headers=headers).status_code for _ in range(4)]
    assert statuses[-1] == 429


def test_reviewer_session_sets_a_httponly_cookie_and_raises_limits(
    clock: Clock,
) -> None:
    app, _ = build_app(make_limits(), clock)
    with TestClient(app) as c:
        response = c.post("/api/v1/reviewer/session", json={"token": REVIEWER_TOKEN})
        assert response.status_code == 200
        cookie = response.headers["set-cookie"].lower()
        assert "httponly" in cookie
        assert "samesite=strict" in cookie
        statuses = [c.get("/ping").status_code for _ in range(8)]
        assert set(statuses) == {200}


def test_wrong_session_token_is_forbidden_and_penalised(clock: Clock) -> None:
    app, _ = build_app(make_limits(), clock)
    with TestClient(app) as c:
        wrong = c.post("/api/v1/reviewer/session", json={"token": "n" * 40})
        assert wrong.status_code == 403
        assert "n" * 40 not in wrong.text


def test_session_is_off_when_no_token_is_configured(clock: Clock) -> None:
    app, _ = build_app(make_limits(), clock, token="")
    with TestClient(app) as c:
        response = c.post("/api/v1/reviewer/session", json={"token": "anything"})
        assert response.status_code == 404


def test_session_rejects_unknown_fields(clock: Clock) -> None:
    app, _ = build_app(make_limits(), clock)
    with TestClient(app) as c:
        response = c.post(
            "/api/v1/reviewer/session", json={"token": REVIEWER_TOKEN, "extra": 1}
        )
        assert response.status_code == 422


def test_diagnostics_reports_how_the_caller_is_identified(client: TestClient) -> None:
    response = client.get(
        "/api/v1/diagnostics/client",
        headers={"x-forwarded-for": "6.6.6.6, 203.0.113.9"},
    )
    body = response.json()
    assert body["client_key"] == "203.0.113.9"
    assert body["forwarded_for_entries"] == 2
    assert body["trusted_proxy_hops"] == 1


def test_slots_are_released_after_each_request(clock: Clock) -> None:
    app, guard = build_app(make_limits(), clock)
    with TestClient(app) as c:
        for index in range(3):
            c.get("/ping", headers={"x-forwarded-for": f"203.0.113.{index}"})
    assert guard.inflight == 0


# --- the real application --------------------------------------------------------
def test_real_app_answers_head_on_health_for_uptime_monitors() -> None:
    with TestClient(real_app) as c:
        assert c.head("/health").status_code == 200
        body = c.get("/health").json()
        assert body["capacity"] == "normal"


def test_real_app_refusals_carry_security_headers() -> None:
    limits = get_settings().limits
    with TestClient(real_app) as c:
        refused = None
        for _ in range(limits.public.general.capacity + 5):
            response = c.get("/api/v1/config/policy")
            if response.status_code == 429:
                refused = response
                break
        assert refused is not None
        assert refused.headers["x-content-type-options"] == "nosniff"
        assert refused.headers["cache-control"] == "no-store"
