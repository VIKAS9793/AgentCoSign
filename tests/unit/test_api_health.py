"""Unit tests for FastAPI gateway endpoints."""

from fastapi.testclient import TestClient

from agentcosign import __version__
from agentcosign.main import create_app


def _client() -> TestClient:
    return TestClient(create_app())


def test_health_check_endpoint() -> None:
    response = _client().get("/health")
    assert response.status_code == 200
    data = response.json()
    assert data["status"] == "healthy"
    assert data["service"] == "AgentCoSign"
    assert data["version"] == __version__
    assert "hermetic" in data
    assert "paypal_execution" in data


def test_get_active_policy_endpoint() -> None:
    response = _client().get("/api/v1/config/policy")
    assert response.status_code == 200
    data = response.json()
    assert data["currency"] == "USD"
    # Money is serialised as strings: no float rounding on the wire.
    assert data["refund_auto_limit"] == "25.00"
    assert data["refund_max_limit"] == "250.00"
    assert data["daily_outflow_cap"] == "1000.00"


def test_policy_endpoint_is_not_cacheable() -> None:
    response = _client().get("/api/v1/config/policy")
    assert response.headers["cache-control"] == "no-store"


def test_security_headers_present() -> None:
    response = _client().get("/health")
    assert response.headers["x-content-type-options"] == "nosniff"
    assert response.headers["x-frame-options"] == "DENY"
    assert "frame-ancestors 'none'" in response.headers["content-security-policy"]


def test_no_cors_headers_are_emitted() -> None:
    """Same-origin only: a cross-origin page must not get CORS approval."""
    response = _client().get("/health", headers={"Origin": "https://evil.example"})
    assert "access-control-allow-origin" not in response.headers
