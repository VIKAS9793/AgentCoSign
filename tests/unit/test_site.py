"""The public pages: landing page, static assets and the API reference."""

import json
import re

import pytest
from fastapi.testclient import TestClient

from agentcosign.main import app
from tests.unit.web_js import run_js

EDGE = {"cf-connecting-ip": "203.0.113.90"}


def get(path: str):  # noqa: ANN201
    with TestClient(app, headers=EDGE) as client:
        return client.get(path)


def test_root_serves_the_landing_page_not_an_error() -> None:
    response = get("/")
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/html")
    assert "AgentCoSign" in response.text
    assert "no real money moves" in response.text


def test_landing_page_keeps_the_strict_policy() -> None:
    policy = get("/").headers["content-security-policy"]
    assert "script-src 'self'" in policy
    assert "cdn.jsdelivr.net" not in policy
    assert "unsafe-inline" not in policy


def test_landing_assets_are_served_with_the_right_types() -> None:
    css = get("/assets/landing.css")
    js = get("/assets/landing.js")
    assert css.status_code == 200
    assert css.headers["content-type"].startswith("text/css")
    assert js.status_code == 200
    assert "fetch(" in js.text


def test_landing_page_renders_untrusted_values_as_text_only() -> None:
    script = get("/assets/landing.js").text
    assert "innerHTML" not in script
    assert "textContent" in script


def test_api_reference_loads_with_a_nonce_and_a_pinned_cdn() -> None:
    response = get("/docs")
    assert response.status_code == 200
    policy = response.headers["content-security-policy"]
    nonce = re.search(r"'nonce-([^']+)'", policy)
    assert nonce is not None
    assert f'<script nonce="{nonce.group(1)}">' in response.text
    assert "swagger-ui-dist@5.17.14" in response.text
    assert "'unsafe-inline'" not in policy.split("style-src")[0]
    assert "frame-ancestors 'none'" in policy


def test_api_reference_nonce_changes_on_every_response() -> None:
    first = get("/docs").headers["content-security-policy"]
    second = get("/docs").headers["content-security-policy"]
    assert first != second


def test_redoc_is_not_exposed() -> None:
    assert get("/redoc").status_code == 404


@pytest.mark.edge
@pytest.mark.parametrize(
    ("execution", "says", "never"),
    [
        ("recorded", "recorded PayPal results", "sandbox"),
        ("sandbox", "PayPal sandbox", "recorded"),
    ],
)
def test_the_landing_page_names_the_mode_the_server_reports(
    execution: str, says: str, never: str
) -> None:
    health = {"paypal_execution": execution, "capacity": "normal"}
    text = run_js(["describe"], f"describe({json.dumps(health)})", "landing.js")
    assert says in text and never not in text


# (what /health says about the models, words that must appear, words that must not)
AI_MODES = [
    ("live", "AI checks run on live models", "are off"),
    ("off", "AI checks are off", "live models"),
]


@pytest.mark.edge
@pytest.mark.parametrize(("models", "says", "never"), AI_MODES)
def test_the_landing_page_names_the_ai_mode_the_server_reports(
    models: str, says: str, never: str
) -> None:
    health = {"paypal_execution": "sandbox", "ai_models": models, "capacity": "normal"}
    text = run_js(["describe"], f"describe({json.dumps(health)})", "landing.js")
    assert says in text and never not in text


@pytest.mark.edge
def test_the_landing_page_leads_into_the_app() -> None:
    page = get("/").text
    assert re.search(r'<a class="start" href="/approve">Open the app</a>', page)
    assert "Try a scenario" in page and "Behind the scenes" in page
    assert "no public way" not in page
    assert get("/approve").status_code == 200
