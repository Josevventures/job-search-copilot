import time
from types import SimpleNamespace

import forms
import pytest

from prep_extract import dom
from prep_extract.browser import Guard
from prep_extract.sanitize import clean


@pytest.fixture
def no_typing(monkeypatch):
    """Every Playwright call that could type, pick, upload or tick is replaced by a recorder."""
    from playwright.sync_api import ElementHandle, Frame, Keyboard, Locator, Page
    calls = []

    def spy(name):
        return lambda *a, **k: calls.append(name)
    for cls in (Page, Frame, Locator, ElementHandle):
        for name in ("fill", "type", "press", "press_sequentially", "set_input_files", "check", "uncheck",
                     "set_checked", "select_option", "tap", "dispatch_event"):
            if hasattr(cls, name):
                monkeypatch.setattr(cls, name, spy(f"{cls.__name__}.{name}"))
    for name in ("type", "press", "insert_text", "down"):
        monkeypatch.setattr(Keyboard, name, spy(f"Keyboard.{name}"))
    return calls


def crawl(site, session, path, deadline=None, **kw):
    page, guard = session
    return dom.crawl(page, site.url(path), guard,
                     deadline=deadline if deadline is not None else time.monotonic() + 60, **kw)


def clicked(got):
    return [(n, label) for n, label, d, _ in got["clicks"] if d == "clicked"]


def denied(got):
    return {clean(label): reason for _, label, d, reason in got["clicks"] if d == "deny"}


def test_multi_step_form_clicks_only_navigation(site, session, no_typing):
    site.pages.update(forms.MULTI_STEP)
    got = crawl(site, session, "/step1")
    assert [n for n, _ in got["pages"]] == [1, 2, 3]
    assert [f["label"] for f in got["pages"][1][1]] == ["Cover letter", "Resume"]
    assert clicked(got) == [(1, "Next"), (2, "Save & continue")]
    d = denied(got)
    assert d["Submit"] == "label contains 'submit'"                 # zero-width space inside
    assert d["Continue"] == "link leaves the posting host"
    assert d["Next"] == "type=submit"
    assert d["Review"] == "label contains 'review'"                 # fullwidth letters
    assert d["Nеxt"].startswith("label has non-ASCII")         # Cyrillic e
    assert d["Review application"] == "label contains 'review'"
    assert d["Submit application"] == "label contains 'submit'"
    assert got["unseen_pages"] is False and got["sign_in_required"] is False and got["errors"] == []
    assert site.posts() == [] and no_typing == []
    assert site.paths() == ["/step1", "/step2", "/step3"]


def test_a_continue_that_secretly_posts_is_stopped_by_the_fence(site, session, no_typing):
    site.pages["/sneaky"] = forms.SNEAKY_POST
    got = crawl(site, session, "/sneaky")
    assert clicked(got) == [(1, "Continue")] and got["unseen_pages"] is True
    assert site.posts() == [] and "/sneaky-submit" not in site.paths() and no_typing == []
    _, guard = session
    assert any(b["method"] == "POST" and b["reason"].startswith(("form submission", "write request"))
               for b in guard.blocked)


def test_script_navigation_off_the_host_is_blocked(site, session):
    site.pages["/off"] = forms.OFFHOST_JS
    got = crawl(site, session, "/off")
    assert clicked(got) == [(1, "Next")] and got["unseen_pages"] is True
    assert "/elsewhere" not in site.paths()
    _, guard = session
    assert any(b["reason"] == "navigation off the allowed hosts blocked" for b in guard.blocked)


def test_required_fields_stop_pagination_and_mark_pages_unseen(site, session, no_typing):
    site.pages["/valid1"] = forms.VALIDATION
    got = crawl(site, session, "/valid1")
    assert [n for n, _ in got["pages"]] == [1] and got["unseen_pages"] is True
    assert got["clicks"][-1][2] == "no_change"
    assert "/valid2" not in site.paths() and no_typing == []


def test_stops_at_five_pages(site, session):
    site.pages.update(forms.LOOP)
    got = crawl(site, session, "/loop1")
    assert [n for n, _ in got["pages"]] == [1, 2, 3, 4, 5] and got["unseen_pages"] is True
    assert got["clicks"][-1][2:] == ("not_clicked", "page limit (5) reached")
    assert site.paths() == ["/loop1", "/loop2", "/loop3", "/loop4", "/loop5"]


def test_step_tabs_are_followed_once(site, session):
    site.pages["/tabs"] = forms.TABS
    got = crawl(site, session, "/tabs")
    assert [[f["label"] for f in fields] for _, fields in got["pages"]] == [["Name"], ["Years of experience"]]
    assert clicked(got) == [(1, "Step 2")] and denied(got)["Submit"] == "label contains 'submit'"


def test_single_page_hint_reads_one_page_and_clicks_nothing(site, session):
    site.pages["/tabs"] = forms.TABS
    got = crawl(site, session, "/tabs", hint={"single_page": True})
    assert [[f["label"] for f in fields] for _, fields in got["pages"]] == [["Name"]]
    assert got["clicks"] == [] and got["unseen_pages"] is False


def test_sign_in_wall_stops_before_any_click(site, session):
    site.pages["/login"] = forms.LOGIN_PAGE
    got = crawl(site, session, "/login")
    assert got["sign_in_required"] is True and got["pages"] == [] and got["clicks"] == []


def test_time_limit_stops_after_the_current_page(site, session):
    site.pages.update(forms.LOOP)
    got = crawl(site, session, "/loop1", deadline=time.monotonic() - 1)
    assert [n for n, _ in got["pages"]] == [1] and got["unseen_pages"] is True
    assert got["errors"] == ["time limit reached; later pages not read"]
    assert site.paths() == ["/loop1"]


@pytest.mark.parametrize("name", ["aria", "title", "before", "imgalt", "bidi"])
def test_a_control_with_a_disguised_name_is_never_clicked(site, session, no_typing, name):
    site.pages["/probe"] = forms.PROBES[name]
    got = crawl(site, session, "/probe")
    assert clicked(got) == [], got
    assert len([r for r in got["clicks"] if r[2] == "deny"]) == 1, got["clicks"]
    assert site.posts() == [] and "/probe-post" not in site.paths() and "/probe-next" not in site.paths()
    assert no_typing == []


def test_a_plain_next_whose_handler_posts_is_blocked_by_the_write_lock(site, session):
    site.pages["/p"] = forms.FETCH_NEXT
    got = crawl(site, session, "/p")
    assert clicked(got) == [(1, "Next")]
    assert site.posts() == []
    _, guard = session
    assert any(b["method"] == "POST" and "write request" in b["reason"] for b in guard.blocked), guard.blocked


def test_xhr_and_beacon_posts_are_blocked_after_the_lock(site, session):
    site.pages["/p"] = forms.BEACON_NEXT
    got = crawl(site, session, "/p")
    assert clicked(got) == [(1, "Next")]
    assert site.posts() == []
    _, guard = session
    assert any(b["url"].endswith("/xhr") and "write request" in b["reason"] for b in guard.blocked), guard.blocked


def test_a_post_needed_to_render_the_first_page_is_still_allowed(site, session):
    site.pages["/p"] = forms.POST_ON_LOAD
    got = crawl(site, session, "/p")
    assert [f["label"] for f in got["pages"][0][1]] == ["Loaded by POST"]
    assert [r["path"] for r in site.posts()] == ["/boot"]


def test_a_refused_navigation_control_marks_pages_unseen(site, session):
    site.pages["/lone"] = forms.LONE_NEXT
    got = crawl(site, session, "/lone")
    assert got["unseen_pages"] is True and clicked(got) == [] and site.posts() == []
    assert denied(got) == {"Next": "type=submit"}


def test_a_sign_in_wall_after_page_one_marks_pages_unseen(site, session):
    site.pages["/start"] = forms.TO_WALL
    site.pages["/login"] = forms.LOGIN_PAGE
    got = crawl(site, session, "/start")
    assert got["sign_in_required"] is True and got["unseen_pages"] is True
    assert [n for n, _ in got["pages"]] == [1]


# ---- Guard.check_page backstop: a stub page puts the "browser" off-host deterministically ----
_DESC = {"idx": 0, "tag": "button", "role": "button", "type": "button", "label": "Next", "labels": ["Next"],
         "bidi": False, "href": None, "target": "", "in_form": False, "in_chrome": False, "form_attrs": False,
         "tab_in_list": False, "selected": False, "disabled": False}


class _Frame:
    def __init__(self, page):
        self.page = page

    def evaluate(self, js, *args):
        self.page.reads.append(self.page.url)
        if "data-px-idx" in js:  # CONTROLS_JS
            return [dict(_DESC)]
        return {"fields": [{"label": "Q", "type": "text", "required": False, "maxlength": None, "options": [],
                            "help_text": ""}], "password": False}


class _StubPage:
    def __init__(self, start_url, after_click_url=None, changed=None):
        self.url, self.start_url, self.after_click_url = "about:blank", start_url, after_click_url
        self.changed = changed
        self.reads, self.clicks = [], 0
        self.main_frame = _Frame(self)
        self.frames = [self.main_frame]

    def goto(self, url, **kw):
        self.url = self.start_url

    def wait_for_load_state(self, *a, **k):
        pass

    def query_selector(self, sel):
        page = self

        def click(**k):
            page.url = page.after_click_url
            page.clicks += 1
        return SimpleNamespace(evaluate=lambda js: {**_DESC, **(page.changed or {})}, click=click)


def test_initial_page_off_host_is_never_read():
    guard = Guard({"jobs.example.com"})
    page = _StubPage("http://evil.example.net/x")
    got = dom.crawl(page, "http://jobs.example.com/a", guard, deadline=time.monotonic() + 60)
    assert got["pages"] == [] and got["clicks"] == [] and got["errors"]
    assert page.reads == [] and page.clicks == 0
    assert any(b["reason"] == "page is off the allowed hosts" for b in guard.blocked)


def test_a_click_that_lands_off_host_stops_the_crawl_unread():
    guard = Guard({"jobs.example.com"})
    page = _StubPage("http://jobs.example.com/a", after_click_url="http://evil.example.net/y")
    got = dom.crawl(page, "http://jobs.example.com/a", guard, deadline=time.monotonic() + 60)
    assert [n for n, _ in got["pages"]] == [1] and got["unseen_pages"] is True and got["errors"]
    assert page.clicks == 1
    assert all("evil.example.net" not in u for u in page.reads)
    assert any(b["reason"] == "page is off the allowed hosts" for b in guard.blocked)


@pytest.mark.parametrize("changed", [{"label": "Submit"}, {"labels": ["Next", "Submit"]}, {"type": "submit"},
                                     {"role": "menuitem"}])
def test_a_control_that_changed_before_the_click_is_not_clicked(changed):
    guard = Guard({"jobs.example.com"})
    page = _StubPage("http://jobs.example.com/a", after_click_url="http://jobs.example.com/b", changed=changed)
    got = dom.crawl(page, "http://jobs.example.com/a", guard, deadline=time.monotonic() + 60)
    assert page.clicks == 0 and got["unseen_pages"] is True
    assert got["clicks"][-1][2:] == ("deny", "control changed before click")
