"""Duplicate-lead matching (2026-10-29): lib/dedupe.py, the intake URL check (leads_intake.ingest), the post-triage
company + title check (fleet_merge.merge_batch) and the Slack reply for a folded lead."""
import json

from lib import dedupe
from lib.lead_store import LeadStore
from lib.slack_replies import Ctx, render
import fleet_merge as fm
import leads_intake as li
from test_intake import fake_fetcher
from test_merge import P, SRC, ckpt, write_ckpt


def rec(lid, company, title, urls=(), locations=(), state="open"):
    return {"lead_id": lid, "company": company, "role_title": title, "urls": list(urls),
            "locations": list(locations), "state": state, "created_at": "", "skipped": state == "skipped"}


# ---- matcher -------------------------------------------------------------------------------------------------
def test_same_company_same_title_punctuation_is_confident():
    a = rec("a", "Quillfeather Games", "Associate Rotational Program - Summer 2027", locations=["Los Angeles, CA"])
    b = rec("b", "Quillfeather Games", "Associate Rotational Program (Summer 2027)", locations=["Los Angeles, USA"])
    assert dedupe.compare(a, b)[0] == "confident"


def test_same_company_different_titles_never_match():
    # one bank's two payments roles, and one company's three Solutions Engineer roles
    boa = [rec("a", "Harborline Bank", "Payments Product Associate"),
           rec("b", "Harborline Bank", "Payments Product Manager")]
    assert dedupe.compare(*boa) is None
    cobalt = [rec(str(i), "Cobaltwing", f"Solutions Engineer, {t}")
              for i, t in enumerate(["Healthcare", "Logistics", "Retail"])]
    for x in cobalt:
        for y in cobalt:
            if x is not y:
                assert dedupe.compare(x, y) is None
    assert dedupe.compare(rec("a", "Acme", "Investment Associate"), rec("b", "Acme", "Senior Investment Associate")) is None


def test_extra_noise_or_company_words_are_fuzzy_not_confident():
    west = (rec("a", "Westfall Ridge Capital", "Investment Associate"),
            rec("b", "Westfall Ridge Capital", "Associate (investment team, NYC)"))
    assert dedupe.compare(*west)[0] == "fuzzy"
    cg = (rec("a", "Civic Growth Fund for Lakeport", "Investment Associate"),
          rec("b", "Civic Partnership for Lakeport (Civic Growth Fund)", "Civic Growth Investment Associate"))
    assert dedupe.compare(*cg)[0] == "fuzzy"


def test_related_company_or_location_conflict_downgrades_to_fuzzy():
    assert dedupe.compare(rec("a", "Northwind Lending", "Associate"), rec("b", "Northwind Lending Ventures", "Associate"))[0] == "fuzzy"
    # a parenthetical alias is the same company (Stonegate Health vs Stonegate Health (Stonegate Ventures))
    assert dedupe.compare(rec("a", "Stonegate Health", "Associate"),
                          rec("b", "Stonegate Health (Stonegate Ventures)", "Associate"))[0] == "confident"
    assert dedupe.compare(rec("a", "Acme", "Associate", locations=["Austin, TX"]),
                          rec("b", "Acme", "Associate", locations=["Boston, MA"]))[0] == "fuzzy"


def test_url_keys_specific_postings_only():
    assert dedupe.url_key("https://www.linkedin.com/jobs/view/senior-associate-at-x-4482742320") == "linkedin-job:4482742320"
    assert dedupe.url_key("https://www.linkedin.com/jobs/view/4482742320/?trk=abc") == "linkedin-job:4482742320"
    assert dedupe.url_key("https://acme.com/careers/") is None
    assert dedupe.url_key("https://acme.com/about/careers") is None
    assert dedupe.url_key("not a url") is None
    same = (rec("a", "Acme", "Associate", urls=["https://acme.com/jobs/123"]),
            rec("b", "Acme Inc (Acme Labs)", "associate", urls=["https://acme.com/jobs/123?utm_source=x"]))
    assert dedupe.compare(*same)[0] == "confident"
    two_roles = (rec("a", "Acme", "Associate", urls=["https://acme.com/jobs/123"]),
                 rec("b", "Acme", "Investment Analyst", urls=["https://acme.com/jobs/123"]))
    assert dedupe.compare(*two_roles)[0] == "fuzzy"   # one post, two roles: never folded
    generic = (rec("a", "Acme", "Analyst", urls=["https://acme.com/careers"]),
               rec("b", "Acme", "Associate", urls=["https://acme.com/careers"]))
    assert dedupe.compare(*generic) is None


def test_best_match_prefers_confident_then_applied():
    new = rec("n", "Acme", "Associate")
    hit = dedupe.best_match(new, [rec("x", "Acme", "Associate (NYC)"), rec("o", "Acme", "Associate"),
                                  rec("p", "Acme", "Associate", state="applied")])
    assert hit[0] == "confident" and hit[1]["lead_id"] == "p"
    assert dedupe.best_match(new, [rec("n", "Acme", "Associate")]) is None   # never itself


def test_report_lists_each_pair_once_and_writes_nothing():
    leads = [{"lead_id": "a", "triage": {"company": "Acme", "role_title": "Associate"}, "intake": {}},
             {"lead_id": "b", "triage": {"company": "Acme", "role_title": "Associate"}, "intake": {}}]
    out = dedupe.report(leads, [{"lead_id": "c", "company": "Acme", "role_title": "Analyst"}])
    assert [(p["a"], p["b"], p["kind"]) for p in out] == [("a", "b", "confident")]


# ---- intake: URL check -----------------------------------------------------------------------------------------
def test_intake_appends_a_second_url_for_a_held_posting_to_the_existing_lead(tmp_dirs):
    s = LeadStore()
    rep = li.ingest(["https://www.linkedin.com/posts/pat_hiring-share-1"], "slack", s, fake_fetcher({}))
    lid = rep["lead_ids"][0]
    s.update(lid, "triage", {"jd_url": "https://www.linkedin.com/jobs/view/4482742320/", "company": "Acme"})
    rep2 = li.ingest(["https://www.linkedin.com/jobs/view/associate-at-acme-4482742320?trk=x"], "slack", s,
                     fake_fetcher({}))
    assert rep2["lead_ids"] == [lid] and rep2["deduped"] == 1 and rep2["created"] == 0
    assert len(list(tmp_dirs["leads"].glob("*.json"))) == 1
    assert len(s.read(lid)["intake"]["sources"]) == 2


def test_intake_skips_a_url_whose_posting_was_skipped(tmp_dirs):
    s = LeadStore()
    s.append_skip({"lead_id": "aaaaaaaaaaaa", "canonical_url": "https://acme.com/jobs/77", "reason": "pay"})
    rep = li.ingest(["https://acme.com/jobs/77/"], "sheet", s, fake_fetcher({}))
    assert rep["created"] == 0 and rep["previously_skipped"] == 1


def test_intake_generic_careers_page_never_dedupes(tmp_dirs):
    s = LeadStore()
    rep = li.ingest(["https://acme.com/careers"], "sheet", s, fake_fetcher({}))
    s.update(rep["lead_ids"][0], "triage", {"jd_url": "https://acme.com/careers"})
    rep2 = li.ingest(["https://acme.com/careers?team=ops"], "sheet", s, fake_fetcher({}))
    assert rep2["created"] == 1 and rep2["deduped"] == 0


# ---- merge: company + title after triage --------------------------------------------------------------------
def _new(store, url):
    lid, _ = store.create_or_merge(url, {**SRC, "url": url})
    return lid


def test_merge_folds_a_confident_duplicate_into_the_existing_lead(tmp_dirs):
    s = LeadStore()
    old = _new(s, "https://quillfeather.example/jobs/1")
    write_ckpt(tmp_dirs, ckpt(old, company="Quillfeather Games", role_title="Associate Rotational Program (Summer 2027)",
                              url="https://quillfeather.example/jobs/1", jd_url="https://quillfeather.example/jobs/1"))
    fm.merge_batch("b1", s, P)
    s.update(old, "decision", {"decision": "applied"})
    new = _new(s, "https://www.linkedin.com/posts/ali_quill-share-9")
    (tmp_dirs["ckpt"] / "b1" / f"{old}.json").unlink()
    write_ckpt(tmp_dirs, ckpt(new, company="Quillfeather Games", role_title="Associate Rotational Program - Summer 2027",
                              url="https://www.linkedin.com/posts/ali_quill-share-9", jd_url="https://quillfeather.example/emerging"))
    rep = fm.merge_batch("b1", s, P)
    assert rep["folded"] == 1
    assert not (tmp_dirs["leads"] / f"{new}.json").exists()
    assert [x["url"] for x in s.read(old)["intake"]["sources"]] == ["https://quillfeather.example/jobs/1",
                                                                     "https://www.linkedin.com/posts/ali_quill-share-9"]
    line = json.loads(s.skips_path.read_text(encoding="utf-8").splitlines()[-1])
    assert (line["lead_id"], line["reason"], line["duplicate_of"], line["auto"]) == (new, "duplicate", old, True)
    assert line["duplicate_state"] == "applied"
    text, final, _ = render({"u": {"status": "ok", "lead_ids": [new]}},
                            Ctx(leads={}, scores={}, run_ok=True, airtable_skipped=False, screen_labels=[],
                                folded={new: line}))
    assert "already applied" in text and "Associate Rotational Program" in text and final


def test_merge_marks_a_fuzzy_duplicate_and_keeps_both(tmp_dirs):
    s = LeadStore()
    a = _new(s, "https://westfall.example/a")
    b = _new(s, "https://www.linkedin.com/posts/westfall-share-2")
    write_ckpt(tmp_dirs, ckpt(a, company="Westfall Ridge Capital", role_title="Investment Associate", jd_url=None))
    write_ckpt(tmp_dirs, ckpt(b, company="Westfall Ridge Capital", role_title="Associate (investment team, NYC)",
                              jd_url=None))
    rep = fm.merge_batch("b1", s, P)
    assert rep["folded"] == 0 and rep["possible_duplicates"] == 1
    ta, tb = s.read(a)["triage"], s.read(b)["triage"]
    # whichever lead merged second points at the first
    assert {ta["possible_duplicate_of"], tb["possible_duplicate_of"]} in ({None, a}, {None, b})
    flagged = ta if ta["possible_duplicate_of"] else tb
    assert flagged["possible_duplicate_note"].startswith("Westfall Ridge Capital — ")


def test_merge_never_folds_a_lead_with_history_or_a_different_title(tmp_dirs):
    s = LeadStore()
    a, b, c = _new(s, "https://cobaltwing.example/1"), _new(s, "https://cobaltwing.example/2"), _new(s, "https://cobaltwing.example/3")
    s.update(c, "decision", {"history": [{"action": "move_up"}]})
    write_ckpt(tmp_dirs, ckpt(a, company="Cobaltwing", role_title="Solutions Engineer, Retail",
                              jd_url="https://cobaltwing.example/1"))
    write_ckpt(tmp_dirs, ckpt(b, company="Cobaltwing", role_title="Solutions Engineer, Healthcare",
                              jd_url="https://cobaltwing.example/2"))
    fm.merge_batch("b1", s, P)
    # c merges after a, whatever order the lead ids sort in: merge walks checkpoints in id order, so a single batch
    # would let a fresh `a` fold into `c` when c's id happens to sort first.
    for lid in (a, b):
        (tmp_dirs["ckpt"] / "b1" / f"{lid}.json").unlink()
    write_ckpt(tmp_dirs, ckpt(c, company="Cobaltwing", role_title="Solutions Engineer, Retail",
                              jd_url="https://cobaltwing.example/3"))
    rep = fm.merge_batch("b1", s, P)
    assert rep["folded"] == 0 and len(list(tmp_dirs["leads"].glob("*.json"))) == 3
    assert s.read(b)["triage"]["possible_duplicate_of"] is None
    assert s.read(c)["triage"]["possible_duplicate_of"] == a   # confident, but c has history: flagged, not folded


def test_dedupe_report_cli_prints_pairs_and_changes_nothing(tmp_dirs, capsys):
    s = LeadStore()
    a, b = _new(s, "https://quillfeather.example/jobs/7"), _new(s, "https://www.linkedin.com/posts/sam_quill-share-3")
    for lid in (a, b):
        s.update(lid, "triage", {"company": "Quillfeather Games", "role_title": "Data Analyst"})
    before = sorted((p.name, p.read_bytes()) for p in tmp_dirs["leads"].iterdir())
    fm.main(["--dedupe-report"])
    out = capsys.readouterr().out
    assert out.count("confident") == 1 and "Quillfeather Games — Data Analyst" in out
    assert sorted((p.name, p.read_bytes()) for p in tmp_dirs["leads"].iterdir()) == before
