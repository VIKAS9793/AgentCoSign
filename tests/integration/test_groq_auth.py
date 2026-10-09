"""Integration test for Groq API authentication and live inference."""

import pytest
import requests

from agentcosign.core.config import get_settings

pytestmark = pytest.mark.integration


def test_groq_authentication_and_models() -> None:
    """Verify live Groq API key authenticates and lists models."""
    get_settings.cache_clear()
    settings = get_settings()

    if not settings.groq_api_key.get_secret_value():
        pytest.skip("GROQ_API_KEY not configured in .env")

    headers = {
        "Authorization": f"Bearer {settings.groq_api_key.get_secret_value()}",
        "Accept": "application/json",
    }

    response = requests.get(
        "https://api.groq.com/openai/v1/models",
        headers=headers,
        timeout=10,
    )

    assert response.status_code == 200, f"Groq Auth failed: {response.text}"
    data = response.json().get("data", [])
    assert len(data) > 0, "No models returned from Groq"
    model_ids = [m["id"] for m in data]
    cfg = settings.models.agent
    assert cfg.primary in model_ids or cfg.fallback in model_ids, (
        "neither configured agent model is available on Groq: "
        f"{cfg.primary}, {cfg.fallback}"
    )


def test_groq_live_inference() -> None:
    """Verify live chat completion inference via Groq."""
    get_settings.cache_clear()
    settings = get_settings()

    if not settings.groq_api_key.get_secret_value():
        pytest.skip("GROQ_API_KEY not configured in .env")

    headers = {
        "Authorization": f"Bearer {settings.groq_api_key.get_secret_value()}",
        "Content-Type": "application/json",
    }
    payload = {
        "model": settings.models.agent.primary,
        "messages": [{"role": "user", "content": "Respond with: AGENTCOSIGN_ONLINE"}],
        "temperature": 0.0,
        "max_tokens": 100,
    }

    response = requests.post(
        "https://api.groq.com/openai/v1/chat/completions",
        headers=headers,
        json=payload,
        timeout=15,
    )

    assert response.status_code == 200, f"Groq Inference failed: {response.text}"
    choices = response.json().get("choices", [])
    assert len(choices) > 0
    content = choices[0]["message"]["content"].strip()
    assert "AGENTCOSIGN_ONLINE" in content.upper()
