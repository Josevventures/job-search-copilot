import json

import openpyxl

from conftest import FIXTURES
from lib.lead_store import LeadStore
import leads_intake as li

SRC_POSTING = {"url": "u", "kind": "posting", "poster_name": None, "poster_url": None, "added_via": "sheet",
               "roundup_of": None, "hint": None}
ROUNDUP_POST = "https://www.linkedin.com/posts/example-recruiter_brightfield-is-hiring-program-managers-share-7000000000000000001-AbCd/"


def fake_fetcher(pages):
    """pages: {url_or_prefix: (final_url, text)}; unknown urls resolve to themselves with empty text."""
    def f(url, timeout=25):
        for k, v in pages.items():
            if url == k or url.startswith(k):
                return v
        return (url, "")
    return f


def test_read_urls_xlsx_ignores_blanks_and_non_urls(tmp_path):
    wb = openpyxl.Workbook()
    ws = wb.active
    for v in ["https://a.com/job/1", None, "  https://a.com/job/1 ", "see below", " https://b.com/careers/2"]:
        ws.append([v])
    p = tmp_path / "x.xlsx"
    wb.save(p)
    urls, ignored = li.read_urls(p, None, [])
    assert urls == ["https://a.com/job/1", "https://a.com/job/1", "https://b.com/careers/2"]
    assert ignored == 1  # "see below"; blank rows are not counted


def test_duplicate_rows_merge_into_one_lead(tmp_dirs):
    s = LeadStore()
    rep = li.ingest(["https://a.com/job/1", "https://a.com/job/1?utm_source=x"], "sheet", s, fake_fetcher({}))
    assert rep["created"] == 1 and rep["merged"] == 1


def test_post_with_single_job_link_becomes_lead_on_posting_url(tmp_dirs):
    html = (FIXTURES / "single_post.html").read_text(encoding="utf-8")
    post = "https://www.linkedin.com/posts/avery-quinn_im-hiring-at-brightfield-share-7000000000000000002-EfGh/"
    posting = "https://careers.brightfield.example/job/1970000000000001"
    s = LeadStore()
    rep = li.ingest([post], "sheet", s, fake_fetcher({post: (post, html), "https://lnkd.in/": (posting, "")}))
    lead = s.read(rep["lead_ids"][0])
    assert lead["canonical_url"] == posting
    assert lead["intake"]["jd_links"] == [posting]
    assert lead["intake"]["sources"][0]["kind"] == "post"
    assert "Quinn" in lead["intake"]["sources"][0]["poster_name"]


def test_same_job_via_post_and_direct_link_is_one_lead(tmp_dirs):
    html = (FIXTURES / "single_post.html").read_text(encoding="utf-8")
    post = "https://www.linkedin.com/posts/avery-quinn_im-hiring-at-brightfield-share-7000000000000000002-EfGh/"
    posting = "https://careers.brightfield.example/job/1970000000000001"
    s = LeadStore()
    rep = li.ingest([post, posting], "sheet", s,
                    fake_fetcher({post: (post, html), "https://lnkd.in/": (posting, "")}))
    assert rep["created"] == 1 and rep["merged"] == 1
    assert len(s.read(rep["lead_ids"][0])["intake"]["sources"]) == 2


def test_roundup_expands_one_lead_per_link(tmp_dirs):
    html = (FIXTURES / "hiring_roundup.html").read_text(encoding="utf-8")
    counter = {"n": 0}

    def f(url, timeout=25):
        if url.startswith("https://www.linkedin.com/posts/example-recruiter"):
            return (url, html)
        if "lnkd.in" in url:
            counter["n"] += 1
            return (f"https://jobright.ai/jobs/info/{counter['n']}", "")
        return (url, "")
    s = LeadStore()
    rep = li.ingest([ROUNDUP_POST], "sheet", s, f)
    assert rep["roundups"] == 1 and rep["created"] == 7
    lead = s.read(rep["lead_ids"][0])
    assert lead["intake"]["sources"][0]["roundup_of"].startswith("https://www.linkedin.com/posts/example-recruiter")
    assert lead["intake"]["sources"][0]["hint"]
    assert all(s.read(i)["intake"]["sources"][0]["poster_name"] for i in rep["lead_ids"])


def test_login_walled_post_still_creates_lead(tmp_dirs):
    wall = ("https://www.linkedin.com/signup/cold-join?session_redirect=https%3A%2F%2Fwww.linkedin.com%2Ffeed"
            "%2Fupdate%2Furn%3Ali%3Ashare%3A7000000000000000004")
    html = (FIXTURES / "login_wall.html").read_text(encoding="utf-8")
    s = LeadStore()
    rep = li.ingest([wall], "sheet", s, fake_fetcher({"https://www.linkedin.com/feed/update": (wall, html)}))
    assert rep["login_walled"] == 1 and rep["created"] == 1
    lead = s.read(rep["lead_ids"][0])
    assert lead["canonical_url"] == "https://www.linkedin.com/feed/update/urn:li:share:7000000000000000004"
    assert lead["intake"]["jd_links"] == []


def test_network_error_still_creates_lead(tmp_dirs):
    s = LeadStore()
    rep = li.ingest(["https://lnkd.in/p/dead"], "sheet", s, fake_fetcher({}))
    assert rep["created"] == 1


def test_screen_export_and_apply(tmp_dirs, tmp_path):
    s = LeadStore()
    rep = li.ingest(["https://a.com/job/1", "https://youtu.be/abc"], "sheet", s, fake_fetcher({}))
    out = tmp_path / "screen_in.json"
    li.export_screen(s, out)
    items = json.loads(out.read_text(encoding="utf-8"))
    assert {i["lead_id"] for i in items} == set(rep["lead_ids"])
    labels = [{"lead_id": items[0]["lead_id"], "label": "job", "reason": "posting"},
              {"lead_id": items[1]["lead_id"], "label": "non_job", "reason": "video"}]
    res = tmp_path / "screen_out.json"
    res.write_text(json.dumps(labels), encoding="utf-8")
    li.apply_screen(s, res)
    assert s.read(items[1]["lead_id"])["intake"]["screen"]["label"] == "non_job"
    li.export_screen(s, out)
    assert json.loads(out.read_text(encoding="utf-8")) == []  # already screened


def test_apply_screen_rejects_unknown_label(tmp_dirs, tmp_path):
    s = LeadStore()
    rep = li.ingest(["https://a.com/job/1"], "sheet", s, fake_fetcher({}))
    res = tmp_path / "bad.json"
    res.write_text(json.dumps([{"lead_id": rep["lead_ids"][0], "label": "maybe", "reason": ""}]), encoding="utf-8")
    li.apply_screen(s, res)
    assert s.read(rep["lead_ids"][0])["intake"].get("screen") is None  # fail open: unknown label ignored


def _post_html(title, links):
    anchors = " ".join(f'<a href="{u}">{u}</a>' for u in links)
    return (f'<html><head><meta property="og:title" content="{title}"></head><body>'
            f'<p class="attributed-text-segment-list__content">Come join us! {anchors}</p></body></html>')


def test_post_with_unresolved_non_job_short_links_is_one_lead(tmp_dirs):
    post = "https://www.linkedin.com/posts/example-poster2_impactjob-share-123-AbCd/"
    html = _post_html("We are hiring | Riley Example", ["https://lnkd.in/a", "https://lnkd.in/b", "https://lnkd.in/c"])
    dest = {"https://lnkd.in/a": "https://myflodesk.com/signup",
            "https://lnkd.in/b": "https://www.linkedin.com/jobs/wayup-jobs",
            "https://lnkd.in/c": "https://joinhandshake.com/events/99"}

    def f(url, timeout=25):
        if url == post:
            return (url, html)
        return (dest.get(url, url), "")
    s = LeadStore()
    rep = li.ingest([post], "sheet", s, f)
    assert rep["roundups"] == 0 and rep["created"] == 1
    lead = s.read(rep["lead_ids"][0])
    assert lead["canonical_url"].startswith("https://www.linkedin.com/posts/example-poster2")
    assert "Come join us" in lead["intake"]["post_text"]
    assert lead["intake"]["jd_links"] == []


def test_apply_screen_skips_bad_rows(tmp_dirs, tmp_path):
    s = LeadStore()
    rep = li.ingest(["https://a.com/job/1"], "sheet", s, fake_fetcher({}))
    good = rep["lead_ids"][0]
    res = tmp_path / "rows.json"
    res.write_text(json.dumps(["junk", {"label": "job"}, {"lead_id": "nope", "label": "job"},
                               {"lead_id": good, "label": "job", "reason": "ok"}]), encoding="utf-8")
    assert li.apply_screen(s, res) == 1
    assert s.read(good)["intake"]["screen"]["label"] == "job"


def test_export_screen_skips_triaged_and_blanks_login_wall(tmp_dirs, tmp_path):
    s = LeadStore()
    done, _ = s.create_or_merge("https://a.com/job/1", SRC_POSTING, jd_links=["https://a.com/job/1"])
    s.update(done, "triage", {"triage_status": "complete"})
    wall, _ = s.create_or_merge("https://www.linkedin.com/feed/update/urn:li:share:1", SRC_POSTING,
                                jd_links=[], post_text="LOGIN_WALLED")
    out = tmp_path / "o.json"
    assert li.export_screen(s, out) == 1
    item = json.loads(out.read_text(encoding="utf-8"))[0]
    assert item["lead_id"] == wall and item["text"] == ""


def test_preview_omitted_when_post_has_no_title_or_description(tmp_dirs):
    post = "https://www.linkedin.com/posts/x_y-share-1-Zz/"
    html = ('<html><body><p class="attributed-text-segment-list__content">hello '
            '<a href="https://boards.greenhouse.io/acme/jobs/1">x</a></p></body></html>')
    s = LeadStore()
    rep = li.ingest([post], "sheet", s, fake_fetcher({post: (post, html)}))
    assert "preview" not in s.read(rep["lead_ids"][0])["intake"]


def test_previously_skipped_url_is_not_recreated(tmp_dirs):
    from lib.canonical import canonicalize, lead_id
    s = LeadStore()
    lid = lead_id(canonicalize("https://a.com/job/1"))
    s.append_skip({"lead_id": lid, "canonical_url": "https://a.com/job/1", "reason": "location"})
    rep = li.ingest(["https://a.com/job/1?utm_source=x", "https://b.com/careers/2"], "sheet", s, fake_fetcher({}))
    assert rep["previously_skipped"] == 1 and rep["created"] == 1 and rep["merged"] == 0
    assert not (tmp_dirs["leads"] / f"{lid}.json").exists()
    assert lid not in rep["lead_ids"]


def test_previously_skipped_roundup_child_is_not_recreated(tmp_dirs):
    from lib.canonical import canonicalize, lead_id
    html = (FIXTURES / "hiring_roundup.html").read_text(encoding="utf-8")
    counter = {"n": 0}

    def f(url, timeout=25):
        if url.startswith("https://www.linkedin.com/posts/example-recruiter"):
            return (url, html)
        if "lnkd.in" in url:
            counter["n"] += 1
            return (f"https://jobright.ai/jobs/info/{counter['n']}", "")
        return (url, "")
    s = LeadStore()
    skipped = lead_id(canonicalize("https://jobright.ai/jobs/info/3"))
    s.append_skip({"lead_id": skipped, "reason": "pay"})
    rep = li.ingest([ROUNDUP_POST], "sheet", s, f)
    assert rep["roundups"] == 1 and rep["created"] == 6 and rep["previously_skipped"] == 1
    assert skipped not in rep["lead_ids"]


def test_report_has_previously_skipped_zero_by_default(tmp_dirs):
    rep = li.ingest(["https://a.com/job/1"], "sheet", LeadStore(), fake_fetcher({}))
    assert rep["previously_skipped"] == 0


from lib.lead_store import LeadStoreError


def raising_fetcher(bad, pages=None):
    base = fake_fetcher(pages or {})

    def f(url, timeout=25):
        if url in bad:
            raise RuntimeError("boom")
        return base(url, timeout)
    return f


def test_one_bad_url_does_not_stop_the_rest(tmp_dirs):
    s = LeadStore()
    urls = ["https://a.com/job/1", "https://bad.com/job/2", "https://c.com/job/3"]
    rep = li.ingest(urls, "slack", s, raising_fetcher({"https://bad.com/job/2"}))
    assert rep["created"] == 2
    assert rep["failed"] == [{"url": "https://bad.com/job/2", "error": "RuntimeError: boom"}]
    assert rep["by_url"]["https://bad.com/job/2"]["status"] == "failed"
    assert rep["by_url"]["https://bad.com/job/2"]["error"] == "RuntimeError: boom"
    assert rep["by_url"]["https://c.com/job/3"] == {"status": "ok", "lead_ids": [rep["lead_ids"][1]], "roundup": False}


def test_store_error_is_isolated(tmp_dirs):
    s = LeadStore()
    real = s.create_or_merge

    def flaky(canonical, source, **kw):
        if "locked" in canonical:
            raise LeadStoreError("lock timeout on x")
        return real(canonical, source, **kw)
    s.create_or_merge = flaky
    rep = li.ingest(["https://locked.com/job/1", "https://ok.com/job/2"], "slack", s, fake_fetcher({}))
    assert rep["created"] == 1
    assert rep["failed"][0]["error"] == "LeadStoreError: lock timeout on x"


def test_roundup_child_failure_keeps_the_other_children(tmp_dirs):
    html = (FIXTURES / "hiring_roundup.html").read_text(encoding="utf-8")
    counter = {"n": 0}

    def f(url, timeout=25):
        if url.startswith("https://www.linkedin.com/posts/example-recruiter"):
            return (url, html)
        if "lnkd.in" in url:
            counter["n"] += 1
            return (f"https://jobright.ai/jobs/info/{counter['n']}", "")
        return (url, "")
    s = LeadStore()
    real = s.create_or_merge

    def flaky(canonical, source, **kw):
        if canonical.endswith("/info/2"):
            raise LeadStoreError("lock timeout")
        return real(canonical, source, **kw)
    s.create_or_merge = flaky
    rep = li.ingest([ROUNDUP_POST], "slack", s, f)
    entry = rep["by_url"][ROUNDUP_POST]
    assert entry["roundup"] is True and entry["status"] == "ok" and len(entry["lead_ids"]) == 6
    assert rep["created"] == 6 and len(rep["failed"]) == 1


def test_previously_skipped_url_status(tmp_dirs):
    from lib.canonical import canonicalize, lead_id
    s = LeadStore()
    s.append_skip({"lead_id": lead_id(canonicalize("https://a.com/job/1")), "reason": "pay"})
    rep = li.ingest(["https://a.com/job/1"], "slack", s, fake_fetcher({}))
    assert rep["by_url"]["https://a.com/job/1"] == {"status": "previously_skipped", "lead_ids": [], "roundup": False}
    assert rep["failed"] == []


def test_note_and_slack_go_into_the_source(tmp_dirs):
    s = LeadStore()
    rep = li.ingest(["https://a.com/job/1"], "slack", s, fake_fetcher({}),
                    note="Dana's team", slack={"channel": "C1", "ts": "1.2"})
    src = s.read(rep["lead_ids"][0])["intake"]["sources"][0]
    assert src["note"] == "Dana's team" and src["slack"] == {"channel": "C1", "ts": "1.2"}
    assert src["added_via"] == "slack"


def test_no_note_means_no_note_key(tmp_dirs):
    s = LeadStore()
    rep = li.ingest(["https://a.com/job/1"], "manual", s, fake_fetcher({}))
    src = s.read(rep["lead_ids"][0])["intake"]["sources"][0]
    assert "note" not in src and "slack" not in src


def test_main_exits_4_only_when_every_url_failed(tmp_dirs, monkeypatch, capsys):
    def all_failed(urls, via, store):
        return {"created": 0, "merged": 0, "roundups": 0, "login_walled": 0, "ignored": 0,
                "previously_skipped": 0, "lead_ids": [], "failed": [{"url": u, "error": "x"} for u in urls],
                "by_url": {u: {"status": "failed", "lead_ids": [], "roundup": False, "error": "x"} for u in urls}}
    monkeypatch.setattr(li, "ingest", all_failed)
    assert li.main(["https://a.com/job/1"]) == 4
    assert '"failed": 1' in capsys.readouterr().out
    monkeypatch.setattr(li, "ingest", lambda urls, via, store: {**all_failed(urls, via, store), "failed": [],
                                                                 "by_url": {}})
    assert li.main(["https://a.com/job/1"]) == 0
