"""Shared setup for the prep extractor tests. Run from the repo root: python -m pytest prep_extract/tests

`site` is a local HTTP server that records every request (method, Host header, path, body), so a test can assert
that nothing was ever POSTed or typed. `session` is the extractor's own fenced browser in offline mode: any request
to a host other than 127.0.0.1 is aborted, so unit tests never reach the internet.

Tests that need a real browser (they use `site` or `session`, or are marked `browser`) are skipped, with the
reason, when Playwright or its Chromium is not installed: CI runs the pure-Python half (schema, sanitize, click
rules, Greenhouse mapping, CLI watchdog), and the DOM half runs wherever Chromium is installed
(`pip install playwright && playwright install chromium`).
"""
import functools
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from types import SimpleNamespace
from urllib.parse import urlsplit

import pytest

BACKEND = Path(__file__).resolve().parents[2]
if str(BACKEND) not in sys.path:
    sys.path.insert(0, str(BACKEND))
FIXTURES = Path(__file__).resolve().parent / "fixtures"


class _Handler(BaseHTTPRequestHandler):
    def _record(self, body: bytes = b"") -> None:
        self.server.log.append({"method": self.command, "host": self.headers.get("Host", ""),
                                "path": urlsplit(self.path).path, "body": body.decode("utf-8", "replace")})

    def do_GET(self):  # noqa: N802
        self._record()
        target = self.server.redirects.get(urlsplit(self.path).path)
        if target is not None:
            self.send_response(302)
            self.send_header("Location", target.replace("__PORT__", str(self.server.server_address[1])))
            self.send_header("Content-Length", "0")
            self.end_headers()
            return
        page = self.server.pages.get(urlsplit(self.path).path)
        if page is None:
            body = b"<!doctype html><html><body><h1>Not found</h1></body></html>"
            self.send_response(404)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        data = page.replace("__PORT__", str(self.server.server_address[1])).encode("utf-8")
        ctype = "application/json" if page.lstrip().startswith(("{", "[")) else "text/html; charset=utf-8"
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_POST(self):  # noqa: N802
        n = int(self.headers.get("Content-Length") or 0)
        self._record(self.rfile.read(n))
        self.send_response(200)
        self.end_headers()
        self.wfile.write(b"submitted")

    do_PUT = do_POST

    def log_message(self, *args):
        pass


@pytest.fixture
def site():
    srv = ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
    srv.pages, srv.log, srv.redirects = {}, [], {}
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    port = srv.server_address[1]
    yield SimpleNamespace(
        port=port, pages=srv.pages, log=srv.log, redirects=srv.redirects,
        url=lambda path: f"http://127.0.0.1:{port}{path}",
        posts=lambda: [r for r in srv.log if r["method"] != "GET"],
        paths=lambda: [r["path"] for r in srv.log if r["path"] != "/favicon.ico"],
        hosts=lambda: {r["host"].split(":")[0] for r in srv.log})
    srv.shutdown()
    srv.server_close()


@pytest.fixture
def session():
    from prep_extract import browser
    with browser.open_session({"127.0.0.1"}, timeout_ms=10000, offline=True) as (page, guard):
        yield page, guard


@functools.lru_cache(maxsize=1)
def browser_missing() -> str | None:
    """None when headless Chromium can start, else why not."""
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        return "playwright is not installed"
    try:
        with sync_playwright() as p:
            if not Path(p.chromium.executable_path).exists():
                return "Playwright's Chromium is not installed (playwright install chromium)"
    except Exception as e:  # noqa: BLE001
        return f"Playwright cannot start: {type(e).__name__}"
    return None


def pytest_collection_modifyitems(config, items):
    needs = [i for i in items if {"site", "session"} & set(getattr(i, "fixturenames", ()))
             or i.get_closest_marker("browser")]
    if not needs:
        return
    why = browser_missing()
    if why:
        for item in needs:
            item.add_marker(pytest.mark.skip(reason=f"needs a browser: {why}"))
