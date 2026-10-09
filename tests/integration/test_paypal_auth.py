"""Integration test for PayPal Sandbox OAuth authentication."""

import pytest
import requests

from agentcosign.core.config import get_settings

pytestmark = pytest.mark.integration


def test_paypal_sandbox_oauth_handshake() -> None:
    """Verify live PayPal Sandbox OAuth handshake with credentials from .env."""
    settings = get_settings()

    if (
        not settings.paypal_client_id
        or not settings.paypal_client_secret.get_secret_value()
    ):
        pytest.skip("PayPal sandbox credentials not set in .env")

    url = "https://api-m.sandbox.paypal.com/v1/oauth2/token"
    headers = {"Accept": "application/json", "Accept-Language": "en_US"}
    data = {"grant_type": "client_credentials"}

    response = requests.post(
        url,
        headers=headers,
        data=data,
        auth=(
            settings.paypal_client_id,
            settings.paypal_client_secret.get_secret_value(),
        ),
        timeout=10,
    )

    assert response.status_code == 200, f"Auth failed: {response.text}"
    token_data = response.json()
    assert token_data.get("token_type") == "Bearer"
    assert "access_token" in token_data
    assert token_data.get("expires_in", 0) > 0
