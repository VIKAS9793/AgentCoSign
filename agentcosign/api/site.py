"""The public face of the service: landing page, static assets and API reference.

The API reference loads Swagger UI from a pinned CDN version. The service-wide
policy forbids third-party scripts, so this one page gets its own policy: scripts
only from the pinned host plus a per-response nonce for the inline start-up code.
"""

import secrets
from pathlib import Path

from fastapi import FastAPI
from fastapi.openapi.docs import get_swagger_ui_html
from fastapi.responses import FileResponse, HTMLResponse, Response
from fastapi.staticfiles import StaticFiles

WEB_DIR = Path(__file__).resolve().parent.parent / "web"
SWAGGER_VERSION = "5.17.14"
SWAGGER_CDN = f"https://cdn.jsdelivr.net/npm/swagger-ui-dist@{SWAGGER_VERSION}"
CDN_HOST = "https://cdn.jsdelivr.net"
ASSET_CACHE = "public, max-age=300"


def _docs_policy(nonce: str) -> str:
    return (
        "default-src 'self'; "
        f"script-src 'nonce-{nonce}' {CDN_HOST}; "
        f"style-src 'self' 'unsafe-inline' {CDN_HOST}; "
        "img-src 'self' data:; connect-src 'self'; "
        "frame-ancestors 'none'; base-uri 'none'; form-action 'self'"
    )


def register_site(app: FastAPI, *, docs_enabled: bool) -> None:
    """Serve the landing page and static assets, and the API reference if enabled."""

    @app.get("/favicon.ico", include_in_schema=False)
    async def favicon() -> Response:
        """Browsers ask for this on every site; answer quietly instead of a 404."""
        return Response(status_code=204, headers={"Cache-Control": ASSET_CACHE})

    if (WEB_DIR / "index.html").is_file():
        app.mount("/assets", StaticFiles(directory=WEB_DIR), name="assets")

        @app.get("/", include_in_schema=False)
        async def landing_page() -> FileResponse:
            """Landing page: what this is, the live status and where to go next."""
            return FileResponse(
                WEB_DIR / "index.html",
                media_type="text/html; charset=utf-8",
                headers={"Cache-Control": "no-cache"},
            )

    if not docs_enabled:
        return

    @app.get("/docs", include_in_schema=False)
    async def api_reference() -> Response:
        """Swagger UI with a nonce so only its own start-up script may run inline."""
        nonce = secrets.token_urlsafe(16)
        page = get_swagger_ui_html(
            openapi_url=app.openapi_url or "/openapi.json",
            title="AgentCoSign API reference",
            swagger_js_url=f"{SWAGGER_CDN}/swagger-ui-bundle.js",
            swagger_css_url=f"{SWAGGER_CDN}/swagger-ui.css",
            swagger_favicon_url="data:,",
        )
        html = bytes(page.body).decode("utf-8")
        html = html.replace("<script>", f'<script nonce="{nonce}">')
        return HTMLResponse(
            html,
            headers={
                "Content-Security-Policy": _docs_policy(nonce),
                "Cache-Control": "no-store",
            },
        )
