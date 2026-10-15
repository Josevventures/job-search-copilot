"""Headless Chromium for the extractor (addendum §3.2/§3.4): a fresh profile every run, never the user's Chrome,
behind a network fence. The fence aborts every non-GET navigation (a form submission), every top-frame navigation
off the allowed hosts, and non-web schemes. offline=True (tests) also aborts every request to another host."""
import re
from contextlib import contextmanager
from urllib.parse import urljoin, urlsplit

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) "
      "Chrome/130.0.0.0 Safari/537.36")
_WEB = ("http", "https")
_OK_SCHEMES = ("http", "https", "data", "blob")
_BAD_LOCATION_RE = re.compile(r"[\\\x00-\x1f\x7f]")
_MAX_HOPS = 10
# The extractor never needs a Worker, and a Worker's WebSocket is not covered by route_web_socket.
_NO_WORKERS_JS = ("for (const n of ['Worker', 'SharedWorker']) { try { Object.defineProperty(window, n, "
                  "{ value: undefined, configurable: false, writable: false }); } catch (e) {} }")


class Guard:
    def __init__(self, allowed_hosts, offline: bool = False):
        self.allowed_hosts = {str(h).lower() for h in allowed_hosts if h}
        self.offline = offline
        self.blocked: list[dict] = []
        self._sockets: list = []
        self.writes_locked = False

    def verdict(self, method: str, url: str, is_navigation: bool, main_frame: bool) -> str | None:
        parts = urlsplit(url)
        host = (parts.hostname or "").lower()
        if parts.scheme not in _OK_SCHEMES:
            return f"scheme {parts.scheme or '?'} not allowed"
        if is_navigation and str(method).upper() != "GET":
            return "form submission (non-GET navigation) blocked"
        if is_navigation and main_frame and parts.scheme in _WEB and host not in self.allowed_hosts:
            return "navigation off the allowed hosts blocked"
        if self.offline and parts.scheme in _WEB and host not in self.allowed_hosts:
            return "offline: external request blocked"
        return None

    def handle(self, route, request) -> None:
        try:
            try:
                main = request.frame.parent_frame is None
            except Exception:
                main = True
            nav = request.is_navigation_request()
            if self.writes_locked and str(request.method).upper() not in ("GET", "HEAD"):
                self._refuse(route, request.method, request.url, "write request after the crawl started blocked")
                return
            reason = self.verdict(request.method, request.url, nav, main)
            if reason:
                self._refuse(route, request.method, request.url, reason)
            elif nav and main and request.resource_type == "document":
                self._fetch_checked(route, request)
            else:
                route.continue_()
        except Exception:  # fail closed: a fence that errors must not let the request through
            try:
                route.abort()
            except Exception:
                pass

    def lock_writes(self) -> None:
        """Called once the first page is read, before any click. A page may need a POST to render (e.g. a GraphQL
        query), but from here on any non-GET/HEAD request of any kind (fetch, XHR, beacon, form) is aborted."""
        self.writes_locked = True

    def _refuse(self, route, method, url, reason) -> None:
        self.blocked.append({"method": method, "url": str(url)[:300], "reason": reason})
        route.abort()

    def _location_reason(self, base: str, location: str) -> tuple[str, str | None]:
        """Resolve one Location header; return (absolute_url, refusal_reason_or_None). Fails closed on anything the
        browser's URL parser could read differently from urlsplit (backslash, control characters, userinfo)."""
        if _BAD_LOCATION_RE.search(location):
            return location, "redirect Location has a backslash or control character"
        target = urljoin(base, location)
        if "@" in urlsplit(target).netloc:
            return target, "redirect Location has userinfo"
        return target, self.verdict("GET", target, True, True)

    def _fetch_checked(self, route, request) -> None:
        """context.route sees only the first URL of a redirect chain, so the guard walks the WHOLE chain itself
        (route.fetch with redirects off), verdicts every Location, and fulfills only the final non-redirect
        response. A 3xx is never handed back to Chromium. Consequence: page.url stays the requested URL and
        relative links in the body resolve against it; the content is the chain's final page."""
        url = request.url
        resp = route.fetch(max_redirects=0)
        for _ in range(_MAX_HOPS):
            loc = resp.headers.get("location")
            if not (300 <= resp.status < 400 and loc):
                route.fulfill(response=resp)
                return
            url, reason = self._location_reason(url, loc)
            if reason:
                self._refuse(route, "GET", url, reason)
                return
            resp = route.fetch(url=url, max_redirects=0)
        self._refuse(route, "GET", url, f"redirect chain longer than {_MAX_HOPS} hops")

    def handle_ws(self, ws) -> None:
        """The extractor needs no WebSocket. A routed socket that is never connect_to_server()'d never reaches the
        real server, so it is only recorded here; ws.close() cannot be called inside this callback (it deadlocks
        the sync API), so close_websockets() closes the queued ones later from the caller's thread."""
        self.blocked.append({"method": "WEBSOCKET", "url": str(ws.url)[:300], "reason": "websocket blocked"})
        self._sockets.append(ws)

    def close_websockets(self) -> None:
        sockets, self._sockets = self._sockets, []
        for ws in sockets:
            try:
                ws.close()
            except Exception:
                pass

    def check_page(self, page) -> bool:
        """Call after every goto and every click: False (and a blocked entry) if the page ended off the allowed
        hosts. Backstop for any navigation path the route fence cannot see."""
        self.close_websockets()
        host = (urlsplit(page.url).hostname or "").lower()
        if host in self.allowed_hosts:
            return True
        self.blocked.append({"method": "GET", "url": str(page.url)[:300], "reason": "page is off the allowed hosts"})
        return False


@contextmanager
def open_session(allowed_hosts, timeout_ms: int = 20000, offline: bool = False):
    """Yield (page, guard). Fresh context: no cookies, no storage, no service workers, downloads refused."""
    from playwright.sync_api import sync_playwright  # lazy: only the extractor venv has Playwright
    guard = Guard(allowed_hosts, offline=offline)
    with sync_playwright() as p:
        b = p.chromium.launch(headless=True)
        try:
            context = b.new_context(accept_downloads=False, service_workers="block", user_agent=UA,
                                    locale="en-US", viewport={"width": 1280, "height": 2000})
            context.route("**/*", guard.handle)
            context.add_init_script(_NO_WORKERS_JS)
            context.route_web_socket("**/*", guard.handle_ws)
            page = context.new_page()
            context.on("page", lambda p: p is not page and p.close())  # popups only; never the working page
            page.on("dialog", lambda dialog: dialog.dismiss())
            page.set_default_timeout(timeout_ms)
            page.set_default_navigation_timeout(timeout_ms)
            yield page, guard
        finally:
            guard.close_websockets()
            b.close()
