"""The public face of the service: landing page, static assets and API reference.

The API reference loads Swagger UI from a pinned CDN version. The service-wide
policy forbids third-party scripts, so this one page gets its own policy: scripts
only from the pinned host plus a per-response nonce for the inline start-up code.
"""

import hashlib
import re
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
# a versioned path never changes content, so it may be kept for a year
VERSIONED_CACHE = "public, max-age=31536000, immutable"
ASSET_NAME = re.compile(r"^[\w-]+\.(js|css)$")
BUILD_FILES = ("approve.html", "approve.js", "approve.css")
ASSET_LINK = re.compile(r'"/assets/([\w.-]+)"')
VERSION_CHARS = 12


def _docs_policy(nonce: str) -> str:
    return (
        "default-src 'self'; "
        f"script-src 'nonce-{nonce}' {CDN_HOST}; "
        f"style-src 'self' 'unsafe-inline' {CDN_HOST}; "
        "img-src 'self' data:; connect-src 'self'; "
        "frame-ancestors 'none'; base-uri 'none'; form-action 'self'"
    )


def _digest(name: str) -> str:
    return hashlib.sha256((WEB_DIR / name).read_bytes()).hexdigest()[:VERSION_CHARS]


def site_build() -> str:
    """Identifies the merchant app as served now, so an open tab can tell that
    the server was updated underneath it."""
    joined = b"".join((WEB_DIR / name).read_bytes() for name in BUILD_FILES)
    return hashlib.sha256(joined).hexdigest()[:VERSION_CHARS]


def _version(match: re.Match[str]) -> str:
    """Link an asset by a path that contains its hash, so a fresh page can never
    be paired with a stale cached script or style after a release."""
    name = match.group(1)
    return f'"/static/{_digest(name)}/{name}"'


def _page(name: str, cache: str) -> HTMLResponse:
    html = ASSET_LINK.sub(_version, (WEB_DIR / name).read_text(encoding="utf-8"))
    html = html.replace(
        '<meta name="build" content="">',
        f'<meta name="build" content="{site_build()}">',
    )
    return HTMLResponse(html, headers={"Cache-Control": cache})


def _register_pages(app: FastAPI) -> None:
    """Static assets, the landing page and the approver page."""
    if not (WEB_DIR / "index.html").is_file():
        return

    @app.get("/static/{digest}/{name}", include_in_schema=False)
    async def versioned_asset(digest: str, name: str) -> Response:
        """A script or style at a path carrying its hash; any other hash is a 404,
        so an old link never receives today's file under a year-long cache."""
        path = WEB_DIR / name
        if not ASSET_NAME.match(name) or not path.is_file() or digest != _digest(name):
            return Response(status_code=404, headers={"Cache-Control": "no-store"})
        return FileResponse(path, headers={"Cache-Control": VERSIONED_CACHE})

    app.mount("/assets", StaticFiles(directory=WEB_DIR), name="assets")

    @app.get("/", include_in_schema=False)
    async def landing_page() -> HTMLResponse:
        """Landing page: what this is, the live status and where to go next."""
        return _page("index.html", "no-cache")

    @app.get("/approve", include_in_schema=False)
    async def approver_page() -> HTMLResponse:
        """The approver's page: enrol a passkey, sign in, approve or decline."""
        return _page("approve.html", "no-store")


def _register_docs(app: FastAPI) -> None:
    """The API reference, with its own content policy."""

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


def register_site(app: FastAPI, *, docs_enabled: bool) -> None:
    """Serve the pages and assets, and the API reference if enabled."""

    @app.get("/favicon.ico", include_in_schema=False)
    async def favicon() -> Response:
        """Browsers ask for this on every site; answer quietly instead of a 404."""
        return Response(status_code=204, headers={"Cache-Control": ASSET_CACHE})

    _register_pages(app)
    if docs_enabled:
        _register_docs(app)
