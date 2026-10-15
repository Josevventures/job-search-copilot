import json
from contextlib import contextmanager
from functools import partial

import forms
from conftest import FIXTURES

from prep_extract import browser, pipeline

MIN = json.loads((FIXTURES / "greenhouse_min.json").read_text(encoding="utf-8"))
GH = "https://job-boards.greenhouse.io/acme/jobs/123"
LEVER = "https://jobs.lever.co/brambleworks/00000000-0000-4000-8000-0000000000aa"


def no_browser(*a, **k):
    raise AssertionError("a browser was opened")


def recording_session(log):
    @contextmanager
    def fake(hosts, timeout_ms=0):
        log.append(set(hosts))
        raise RuntimeError("no browser in this test")
        yield  # pragma: no cover
    return fake


def boom(url, timeout):
    raise RuntimeError("API down")


OFFLINE = partial(browser.open_session, offline=True)


def test_greenhouse_url_uses_the_api_and_never_opens_a_browser():
    seen = []
    r = pipeline.extract(GH, fetch_json=lambda u, timeout: seen.append(u) or MIN, session=no_browser)
    assert seen == ["https://boards-api.greenhouse.io/v1/boards/acme/jobs/123?questions=true"]
    assert (r["adapter"], r["ats"], r["questions_visible"]) == ("greenhouse_api", "greenhouse", True)


def test_api_failure_falls_through_to_dom_on_the_posting_host():
    log = []
    r = pipeline.extract(GH, fetch_json=boom, session=recording_session(log))
    assert log == [{"job-boards.greenhouse.io"}]
    assert r["ats"] == "greenhouse" and r["questions_visible"] is False and r["adapter"] is None
    assert r["errors"][0] == "greenhouse_api: RuntimeError: API down"
    assert r["errors"][1] == "generic_dom: RuntimeError: no browser in this test"


def test_api_response_without_questions_falls_through():
    log = []
    r = pipeline.extract(GH, fetch_json=lambda u, timeout: {"title": "x"}, session=recording_session(log))
    assert r["errors"][0].startswith("greenhouse_api: ValueError") and len(log) == 1


def test_api_returning_a_non_dict_falls_through_instead_of_raising():
    log = []
    r = pipeline.extract(GH, fetch_json=lambda u, timeout: ["not", "a", "job"], session=recording_session(log))
    assert r["errors"][0].startswith("greenhouse_api:") and len(log) == 1


def test_lever_tries_its_site_adapter_then_generic():
    log = []
    r = pipeline.extract(LEVER, fetch_json=no_browser, session=recording_session(log))
    assert log == [{"jobs.lever.co"}, {"jobs.lever.co"}]
    assert [e.split(":")[0] for e in r["errors"]] == ["lever_dom", "generic_dom"]
    assert r["ats"] == "lever"


def test_non_http_url_is_rejected_without_any_fetch():
    r = pipeline.extract("javascript:alert(1)", fetch_json=no_browser, session=no_browser)
    assert r["errors"] == ["not an http(s) URL"] and r["questions_visible"] is False


def test_generic_adapter_on_a_local_form(site):
    site.pages["/form"] = forms.SIMPLE_FORM
    r = pipeline.extract(site.url("/form"), session=OFFLINE)
    assert (r["adapter"], r["ats"], r["questions_visible"]) == ("generic_dom", None, True)
    assert [f["label"] for f in r["pages"][0]["fields"]] == forms.SIMPLE_LABELS
    assert [c["decision"] for c in r["click_log"]] == ["deny"]          # "Submit application"
    assert site.posts() == []


def test_a_json_board_page_is_not_a_form(site):
    site.pages["/board"] = '{"jobs": []}'
    r = pipeline.extract(site.url("/board"), session=OFFLINE)
    assert r["questions_visible"] is False
    assert r["errors"] == [f"generic_dom: no form fields found at {site.url('/board')}"]


def test_a_closed_posting_404_is_not_a_form(site):
    r = pipeline.extract(site.url("/missing"), session=OFFLINE)
    assert r["questions_visible"] is False and r["errors"][0].startswith("generic_dom: no form fields found")


def test_a_sign_in_wall_is_reported_not_crawled(site):
    site.pages["/login"] = forms.LOGIN_PAGE
    r = pipeline.extract(site.url("/login"), session=OFFLINE)
    assert r["sign_in_required"] is True and r["questions_visible"] is False and r["adapter"] == "generic_dom"
    assert site.posts() == []
