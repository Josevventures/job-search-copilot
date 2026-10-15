import forms
import pytest

from prep_extract import browser, dom, schema


@pytest.mark.parametrize("method,url,nav,main,offline,expected", [
    ("GET", "https://jobs.example.com/a", True, True, False, None),
    ("POST", "https://jobs.example.com/apply", True, True, False, "form submission (non-GET navigation) blocked"),
    ("POST", "https://jobs.example.com/apply", True, False, False, "form submission (non-GET navigation) blocked"),
    ("GET", "https://evil.com/x", True, True, False, "navigation off the allowed hosts blocked"),
    ("GET", "https://cdn.other.com/app.js", False, True, False, None),
    ("POST", "https://jobs.example.com/graphql", False, True, False, None),
    ("GET", "https://cdn.other.com/app.js", False, True, True, "offline: external request blocked"),
    ("GET", "ftp://jobs.example.com/x", False, True, False, "scheme ftp not allowed"),
    ("GET", "file:///c:/secret.txt", True, True, False, "scheme file not allowed"),
])
def test_guard_verdicts(method, url, nav, main, offline, expected):
    g = browser.Guard({"jobs.example.com"}, offline=offline)
    assert g.verdict(method, url, nav, main) == expected


def _read(site, session, path, html):
    site.pages[path] = html
    page, _ = session
    page.goto(site.url(path), wait_until="domcontentloaded")
    dom.settle(page)
    return dom.read_page(page)


def test_simple_form_fields_in_order_with_labels_limits_and_options(site, session):
    snap = _read(site, session, "/form", forms.SIMPLE_FORM)
    r = schema.new_result(site.url("/form"))
    schema.add_page(r, 1, snap["fields"])
    r = schema.finalize(r)
    fields = r["pages"][0]["fields"]
    assert [f["label"] for f in fields] == forms.SIMPLE_LABELS
    assert [f["type"] for f in fields] == ["text", "email", "tel", "text", "textarea", "textarea", "textarea",
                                           "file", "select", "radio", "text"]
    assert [f["required"] for f in fields] == [True, True, False, True, False, False, False, True, True, False, False]
    by = {f["label"]: f for f in fields}
    assert by["Why Acme?"]["max_chars"] == 1500 and by["Why Acme?"]["help_text"] == "Keep it short."
    assert by["Tell us about a hard problem"]["max_words"] == 200
    assert by["Cover letter"]["max_chars"] == 3000
    assert by["Work authorization"]["options"] == ["Yes, I am authorized", "No"]
    assert by["Willing to relocate?"]["options"] == ["Yes", "No"]
    assert r["cover_letter"] == {"mode": "text", "required": False}
    assert r["resume"] == {"present": True, "required": True}
    assert snap["password"] is False and dom.is_gate(snap) is False


def test_reading_a_form_never_posts_or_types(site, session):
    _read(site, session, "/form", forms.SIMPLE_FORM)
    assert not _wait_typed(site, session[0], 2.0)   # same window as the positive control
    assert site.posts() == [] and "/typed" not in site.paths()


def test_controls_report_implicit_submit_types(site, session):
    snap = _read(site, session, "/form", forms.SIMPLE_FORM)
    by = {c["label"]: c for c in snap["controls"]}
    assert by["Submit application"]["type"] == "submit"   # typeless <button> inside a <form>
    assert by["Back to jobs"]["type"] == "button"         # typeless <button> outside any form
    page, _ = session
    idx = by["Back to jobs"]["idx"]
    assert page.locator(f'[data-px-idx="{idx}"]').count() == 1


@pytest.mark.parametrize("html,gate", [
    (forms.LOGIN_PAGE, True),
    (forms.CREATE_ACCOUNT_PAGE, True),
    (forms.EMAIL_GATE_PAGE, True),
    (forms.SSO_FORM, False),
])
def test_sign_in_detection(site, session, html, gate):
    snap = _read(site, session, "/p", html)
    assert dom.is_gate(snap) is gate
    assert site.posts() == []


def test_offline_session_blocks_external_requests(site, session):
    _read(site, session, "/img", forms.EXTERNAL_IMAGE_PAGE)
    _, guard = session
    assert any("example.com" in b["url"] and b["reason"].startswith("offline") for b in guard.blocked)


# ---- fix round 1: fence integration tests (real Chromium; server must never see it AND guard must record it) ----
from types import SimpleNamespace


# Playwright's sync API only services route callbacks while the test thread is inside a Playwright call, so every
# wait below uses page.wait_for_timeout (never time.sleep, which would let requests sit unsent and prove nothing).
def _wait_typed(site, page, wait: float):
    for _ in range(int(wait * 10)):
        if "/typed" in site.paths():
            return True
        page.wait_for_timeout(100)
    return "/typed" in site.paths()


def _goto_quiet(page, url):
    try:
        page.goto(url, wait_until="domcontentloaded")
    except Exception:
        pass
    page.wait_for_timeout(700)


def test_typed_detector_positive_control(site, session):
    _read(site, session, "/form", forms.SIMPLE_FORM)
    page, _ = session
    page.fill("#fn", "x")
    assert _wait_typed(site, page, 2.0), "the /typed detector cannot see typing, so the nothing-typed test proves nothing"


def test_autosubmitting_form_is_blocked(site, session):
    site.pages["/auto"] = forms.AUTOSUBMIT_PAGE
    page, guard = session
    _goto_quiet(page, site.url("/auto"))
    assert site.posts() == [] and "/steal" not in site.paths()
    assert any(b["method"] == "POST" and b["reason"].startswith("form submission") for b in guard.blocked)


def test_script_navigation_off_host_is_blocked(site, session):
    site.pages["/nav"] = forms.NAV_OFFHOST_PAGE
    page, guard = session
    _goto_quiet(page, site.url("/nav"))
    assert "/landed" not in site.paths() and "localhost" not in site.hosts()
    assert any("localhost" in b["url"] and "off the allowed hosts" in b["reason"] for b in guard.blocked)


def test_redirect_off_host_is_blocked(site, session):
    site.redirects["/redir"] = "http://localhost:__PORT__/landed"
    site.pages["/landed"] = forms.page("<p>landed</p>")
    page, guard = session
    _goto_quiet(page, site.url("/redir"))
    assert "/landed" not in site.paths() and "localhost" not in site.hosts()
    assert any("localhost" in b["url"] and "off the allowed hosts" in b["reason"] for b in guard.blocked)
    assert "localhost" not in page.url          # the page never left the allowed host


def test_redirect_on_host_is_followed(site, session):
    site.redirects["/redir"] = "http://127.0.0.1:__PORT__/landed"
    site.pages["/landed"] = forms.page("<p>landed</p>")
    page, guard = session
    page.goto(site.url("/redir"), wait_until="domcontentloaded")
    # the guard follows the chain itself and fulfills the final body, so page.url stays the requested URL
    assert "landed" in page.content() and guard.blocked == [] and guard.check_page(page) is True


def test_check_page_flags_an_off_host_page(site):
    g = browser.Guard({"jobs.example.com"})
    assert g.check_page(SimpleNamespace(url="https://jobs.example.com/a")) is True
    assert g.check_page(SimpleNamespace(url="https://evil.com/a")) is False
    assert g.blocked and g.blocked[-1]["url"].startswith("https://evil.com")


def test_websocket_is_closed_and_recorded(site, session):
    site.pages["/ws-page"] = forms.WS_PAGE
    page, guard = session
    _goto_quiet(page, site.url("/ws-page"))
    assert "/ws" not in site.paths()
    assert any(b["method"] == "WEBSOCKET" for b in guard.blocked)


def test_handle_fails_closed_when_verdict_raises():
    class Route:
        aborted = continued = False
        def abort(self): self.aborted = True
        def continue_(self): self.continued = True
    class Req:
        method, url = "GET", "https://a.com/"
        @property
        def frame(self): raise RuntimeError("boom")
        def is_navigation_request(self): raise RuntimeError("boom")
    r = Route()
    browser.Guard({"a.com"}).handle(r, Req())
    assert r.aborted and not r.continued


def test_popups_are_closed_but_the_main_page_survives(site, session):
    site.pages["/pop"] = forms.page("<script>window.open('/p2')</script>")
    site.pages["/p2"] = forms.page("<p>popup</p>")
    page, _ = session
    _goto_quiet(page, site.url("/pop"))
    assert not page.is_closed()
    assert len(page.context.pages) == 1


def test_gate_false_positives_and_true_positive(site, session):
    for html, gate in [(forms.JD_PAGE, False), (forms.STEP_PAGE, False), (forms.LOGIN_PAGE, True)]:
        assert dom.is_gate(_read(site, session, "/g", html)) is gate


def test_long_page_strings_are_capped_in_page(site, session):
    site.pages["/long"] = forms.page("<label for='q'>" + "A" * 50000 + "</label><input id='q'>")
    snap = _read(site, session, "/long", site.pages["/long"])
    assert len(snap["fields"][0]["label"]) <= 2000


def test_read_page_reports_frame_errors_and_survives_controls_failure():
    class Frame:
        def evaluate(self, *a): raise RuntimeError("detached")
    page = SimpleNamespace(frames=[Frame()], main_frame=Frame(), url="http://x/")
    snap = dom.read_page(page)
    assert snap["fields"] == [] and snap["controls"] == []
    assert len(snap["errors"]) == 2 and "detached" in snap["errors"][0]


# ---- fix round 2 ----
@pytest.mark.parametrize("name,location", [
    ("backslash", r"http://localhost:__PORT__\@127.0.0.1:__PORT__/landed"),
    ("userinfo", "http://127.0.0.1:__PORT__@localhost:__PORT__/landed"),
    ("tab", "http://local	host:__PORT__/landed"),
])
def test_malformed_or_disguised_location_is_refused(site, session, name, location):
    site.redirects["/redir"] = location
    site.pages["/landed"] = forms.page("<p>landed</p>")
    page, guard = session
    _goto_quiet(page, site.url("/redir"))
    assert "/landed" not in site.paths() and "localhost" not in site.hosts()
    assert guard.blocked and "location" in guard.blocked[-1]["reason"].lower()
    assert "localhost" not in page.url


def test_multi_hop_redirect_chain_is_checked_hop_by_hop(site, session):
    site.redirects["/r1"] = "http://127.0.0.1:__PORT__/r2"
    site.redirects["/r2"] = "http://localhost:__PORT__/landed"
    site.pages["/landed"] = forms.page("<p>landed</p>")
    page, guard = session
    _goto_quiet(page, site.url("/r1"))
    assert "/landed" not in site.paths() and "localhost" not in site.hosts()
    assert any("localhost" in b["url"] and "off the allowed hosts" in b["reason"] for b in guard.blocked)
    assert "localhost" not in page.url


def test_redirect_loop_is_cut_off(site, session):
    site.redirects["/loop"] = "http://127.0.0.1:__PORT__/loop"
    page, guard = session
    _goto_quiet(page, site.url("/loop"))
    assert any("redirect" in b["reason"] for b in guard.blocked)


def test_workers_are_unavailable_so_no_websocket_from_a_worker(site, session):
    site.pages["/w"] = forms.page("<script>try { new Worker('/wjs'); } catch (e) { document.title = 'noworker'; }"
                                  "try { new SharedWorker('/wjs'); } catch (e) { document.title += 'noshared'; }</script>")
    site.pages["/wjs"] = "new WebSocket('ws://localhost:__PORT__/ws');"
    page, guard = session
    _goto_quiet(page, site.url("/w"))
    assert "/wjs" not in site.paths() and "/ws" not in site.paths() and "localhost" not in site.hosts()
    assert page.title() == "noworkernoshared"


@pytest.mark.parametrize("html", [forms.DIV_HEADER_PAGE, forms.APPLIED_PAGE, forms.DIV_FOOTER_STEP_PAGE])
def test_gate_not_fooled_by_div_chrome_or_body_login_hint(site, session, html):
    assert dom.is_gate(_read(site, session, "/g", html)) is False
