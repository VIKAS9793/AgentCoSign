"""Shared pytest fixtures for AgentCoSign."""

import pytest
from fastapi.testclient import TestClient

from agentcosign.core.config import Settings, get_settings
from agentcosign.main import create_app


@pytest.fixture
def test_settings() -> Settings:
    """Fixture providing test configuration settings."""
    return get_settings()


@pytest.fixture
def test_client() -> TestClient:
    """Fixture providing FastAPI test client."""
    app = create_app()
    return TestClient(app)
