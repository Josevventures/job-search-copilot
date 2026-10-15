import time

import pytest
from conftest import FIXTURES

from prep_extract import dom, schema, sites

LEVER_POST = "https://jobs.lever.co/brambleworks/00000000-0000-4000-8000-0000000000aa"
ASHBY_POST = "https://jobs.ashbyhq.com/AshgroveLabs/00000000-0000-4000-8000-0000000000bb"
ASHBY_APP = "https://jobs.ashbyhq.com/Kiteline/00000000-0000-4000-8000-0000000000cc/application"


@pytest.mark.parametrize("url,adapter,apply_url", [
    (LEVER_POST, sites.LEVER, LEVER_POST + "/apply"),
    (LEVER_POST + "/apply?lever-source=LinkedIn", sites.LEVER, LEVER_POST + "/apply"),
    (ASHBY_POST, sites.ASHBY, ASHBY_POST + "/application"),
    (ASHBY_APP, sites.ASHBY, ASHBY_APP),
])
def test_site_adapters_build_the_apply_url(url, adapter, apply_url):
    assert sites.match(url) is adapter
    assert adapter.apply_url(url) == apply_url


@pytest.mark.parametrize("url", [
    "https://api.ashbyhq.com/posting-api/job-board/exampleco",
    "https://jobs.lever.co/brambleworks",
    "https://jobs.lever.co.evil.com/brambleworks/00000000-0000-4000-8000-0000000000aa",
    "https://job-boards.greenhouse.io/examplecorp/jobs/4000000009",
    "https://www.linkedin.com/jobs/view/1",
])
def test_other_urls_get_no_site_adapter(url):
    assert sites.match(url) is None
    assert sites.GENERIC.apply_url(url) == url


@pytest.mark.parametrize("host,ats", [
    ("jobs.lever.co", "lever"), ("jobs.ashbyhq.com", "ashby"), ("job-boards.greenhouse.io", "greenhouse"),
    ("exampleco.wd1.myworkdayjobs.com", "workday"), ("www.linkedin.com", "linkedin"),
    ("evilgreenhouse.io", None), ("example.com", None), (None, None),
])
def test_ats_for(host, ats):
    assert sites.ats_for(host) == ats


@pytest.mark.parametrize("name,adapter", [("lever_apply.html", sites.LEVER), ("ashby_application.html", sites.ASHBY)])
def test_real_fixture_maps_the_form(site, session, name, adapter):
    site.pages["/apply"] = (FIXTURES / name).read_text(encoding="utf-8")
    page, guard = session
    got = dom.crawl(page, site.url("/apply"), guard, deadline=time.monotonic() + 60, hint=adapter.hint)
    r = schema.new_result(site.url("/apply"))
    for n, fields in got["pages"]:
        schema.add_page(r, n, fields)
    r = schema.finalize(r)
    labels = [f["label"] for p in r["pages"] for f in p["fields"]]
    assert len(labels) >= 3 and all(labels), labels
    joined = " | ".join(labels).lower()
    assert "name" in joined and "email" in joined, labels
    assert r["resume"]["present"] is True, labels
    assert got["sign_in_required"] is False
    assert [c for c in got["clicks"] if c[2] == "clicked"] == []   # single-page forms: nothing to click
    assert site.posts() == []


def test_ashby_yes_no_buttons_come_out_as_a_yes_no_radio(site, session):
    site.pages["/apply"] = (FIXTURES / "ashby_application.html").read_text(encoding="utf-8")
    page, guard = session
    got = dom.crawl(page, site.url("/apply"), guard, deadline=time.monotonic() + 60, hint=sites.ASHBY.hint)
    fields = [f for _, fs in got["pages"] for f in fs]
    auth = [f for f in fields if f["label"].startswith("Are you legally authorized")]
    assert len(auth) == 1 and auth[0]["type"] == "radio" and auth[0]["options"] == ["Yes", "No"], auth
    assert not [f for f in fields if f["options"] == ["on"]]


def _crawl_fixture(site, session, name, adapter):
    site.pages["/apply"] = (FIXTURES / name).read_text(encoding="utf-8")
    page, guard = session
    got = dom.crawl(page, site.url("/apply"), guard, deadline=time.monotonic() + 60, hint=adapter.hint)
    r = schema.new_result(site.url("/apply"))
    for n, fields in got["pages"]:
        schema.add_page(r, n, fields)
    return got, schema.finalize(r)


def test_ashby_fieldset_radio_without_legend_takes_the_question_title(site, session):
    # Seen in acceptance 2026-10-15 on a live Ashby form: <fieldset> with no <legend>; the question title is a sibling <label>
    got, r = _crawl_fixture(site, session, "ashby_fieldset_radio.html", sites.ASHBY)
    fields = [f for p in r["pages"] for f in p["fields"]]
    visa = [f for f in fields if f["options"] == ["Yes", "No"]]
    assert len(visa) == 1, fields
    assert visa[0]["label"] == "Visa Support" and visa[0]["type"] == "radio"
    assert "visa support to work in the U.S." in (visa[0]["help_text"] or "")
    assert all(f["label"] and "label_missing" not in f for f in fields), fields
    assert [c for c in got["clicks"] if c[2] == "clicked"] == [] and site.posts() == []


def test_lever_resume_star_marker_means_required_and_leaves_the_label(site, session):
    _, r = _crawl_fixture(site, session, "lever_apply.html", sites.LEVER)
    fields = [f for p in r["pages"] for f in p["fields"]]
    resume = [f for f in fields if f["type"] == "file"]
    assert len(resume) == 1 and resume[0]["label"] == "Resume/CV" and resume[0]["required"] is True, resume
    assert r["resume"] == {"present": True, "required": True}
    assert not [f for f in fields if "✱" in f["label"]]
