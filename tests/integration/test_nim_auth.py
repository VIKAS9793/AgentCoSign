"""Integration test for NVIDIA NIM API authentication and live inference."""

import pytest
import requests

from agentcosign.core.config import get_settings

pytestmark = pytest.mark.integration


def test_nvidia_nim_authentication_and_models() -> None:
    """Verify live NVIDIA NIM API key authenticates and lists models."""
    get_settings.cache_clear()
    settings = get_settings()

    if not settings.nvidia_nim_api_key.get_secret_value():
        pytest.skip("NVIDIA_NIM_API_KEY not configured in .env")

    headers = {
        "Authorization": f"Bearer {settings.nvidia_nim_api_key.get_secret_value()}",
        "Accept": "application/json",
    }

    response = requests.get(
        "https://integrate.api.nvidia.com/v1/models",
        headers=headers,
        timeout=10,
    )

    assert response.status_code == 200, f"NIM Auth failed: {response.text}"
    data = response.json().get("data", [])
    assert len(data) > 0, "No models returned from NVIDIA NIM"
    model_ids = [m["id"] for m in data]
    cfg = settings.models.extraction
    assert cfg.primary in model_ids or cfg.fallback in model_ids, (
        f"configured extraction models not listed: {cfg.primary}, {cfg.fallback}"
    )


def test_nvidia_nim_live_inference() -> None:
    """Verify live chat completion inference via NVIDIA NIM."""
    get_settings.cache_clear()
    settings = get_settings()

    if not settings.nvidia_nim_api_key.get_secret_value():
        pytest.skip("NVIDIA_NIM_API_KEY not configured in .env")

    headers = {
        "Authorization": f"Bearer {settings.nvidia_nim_api_key.get_secret_value()}",
        "Content-Type": "application/json",
    }
    payload = {
        "model": settings.models.extraction.primary,
        "messages": [
            {"role": "user", "content": "Respond with the single word: READY"}
        ],
        "temperature": 0.0,
        "max_tokens": 10,
    }

    response = requests.post(
        "https://integrate.api.nvidia.com/v1/chat/completions",
        headers=headers,
        json=payload,
        timeout=15,
    )

    assert response.status_code == 200, f"NIM Inference failed: {response.text}"
    choices = response.json().get("choices", [])
    assert len(choices) > 0
    content = choices[0]["message"]["content"].strip()
    assert "READY" in content.upper()
