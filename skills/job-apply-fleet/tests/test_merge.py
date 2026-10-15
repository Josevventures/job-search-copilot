import json

import pytest

from lib.checkpoint import validate
from lib.lead_store import LeadStore
from lib.prefs import load
import fleet_merge as fm

P = load()
SRC = {"url": "https://a.com/job/1", "kind": "posting", "poster_name": None, "poster_url": None,
       "added_via": "sheet", "roundup_of": None, "hint": None}


def ckpt(lid, **kw):
    d = {"url": "https://a.com/job/1", "lead_id": lid, "batch_id": "b1", "status": "complete",
         "checkpoint_version": 3, "fetch_method": "webfetch", "fetched_at": "2026-10-13T00:00:00Z",
         "company": "Acme", "role_title": "Associate", "location": "New York, NY (hybrid)", "pay": "$150K",
         "pay_type": "salary", "jd_url": "https://a.com/job/1", "jd_read": True, "jd_note": None,
         "posting_status": "open", "fit_score": "Medium", "fit_rationale": "ok",
         "network": {"contacts": [], "summary": "none"},
         "engage": {"name": "Pat", "url": "https://www.linkedin.com/posts/pat", "note": None},
         "airtable_match": {"relation": "none", "record_id": None, "note": None},
         "eligibility_note": None, "alternates": [], "draft_answers": None,
         "job_summary": "Own the grid analytics roadmap", "main_gap": "No utility-customer experience",
         "scores": {"role_fit": {"score": 72, "evidence": "B2B product record"},
                    "domain": {"score": 90, "evidence": "climate software"}},
         "level": "senior", "locations": [{"label": "New York, NY", "metro": "new_york"}], "modality": "hybrid",
         "relocation_assistance": None,
         "pay_bands": [{"location_metro": "new_york", "min": 140000, "max": 160000, "period": "year", "basis": "base"}],
         "pay_source": "posting", "bonus": True, "equity": None, "posted_date": "2026-10-10",
         "posted_date_source": "posting", "reposted": False, "applicant_count": None, "inbound": None}
    d.update(kw)
    return d


def write_ckpt(tmp_dirs, d):
    p = tmp_dirs["ckpt"] / "b1"
    p.mkdir(exist_ok=True)
    (p / f"{d['lead_id']}.json").write_text(json.dumps(d), encoding="utf-8")


def test_validate_ok_and_problems():
    assert validate(ckpt("x")) == []
    assert validate({"url": "u", "lead_id": "x", "batch_id": "b", "status": "failed",
                     "fetch_method": "webfetch", "error": "fetch_failed"}) == []


@pytest.mark.parametrize("kw,needle", [
    ({"checkpoint_version": 2}, "stale_prompt"),
    ({"scores": {"role_fit": {"score": 101, "evidence": "x"}, "domain": {"score": 50, "evidence": "x"}}}, "scores.role_fit"),
    ({"scores": {"role_fit": {"score": 50, "evidence": ""}, "domain": {"score": 50, "evidence": "x"}}}, "scores.role_fit"),
    ({"scores": {"role_fit": {"score": True, "evidence": "x"}, "domain": {"score": 50, "evidence": "x"}}}, "scores.role_fit"),
    ({"scores": {"role_fit": {"score": 50, "evidence": "x"}}}, "scores.domain"),
    ({"job_summary": "x" * 121}, "job_summary"),
    ({"main_gap": "x" * 121}, "main_gap"),
    ({"level": "vp"}, "level"),
    ({"modality": "sometimes"}, "modality"),
    ({"locations": [{"label": "NYC", "metro": "gotham"}]}, "locations[0]"),
    ({"pay_bands": [{"location_metro": "new_york", "min": 9, "max": 5, "period": "year", "basis": "base"}]}, "pay_bands[0]"),
    ({"pay_bands": [{"location_metro": "new_york", "min": 1, "max": 5, "period": "week", "basis": "base"}]}, "pay_bands[0]"),
    ({"pay_bands": [{"location_metro": "new_york", "min": 60, "max": 70, "period": "year", "basis": "base"}]}, "pay_bands[0]"),
    ({"pay_bands": [{"location_metro": "new_york", "min": 300, "max": 600, "period": "hour", "basis": "base"}]}, "pay_bands[0]"),
    ({"pay_bands": [{"location_metro": "new_york", "min": 900, "max": 1200, "period": "month", "basis": "base"}]}, "pay_bands[0]"),
    ({"pay_bands": [{"location_metro": "new_york", "min": 1, "max": 5, "period": [], "basis": "base"}]}, "pay_bands[0]"),
    ({"pay_bands": [{"location_metro": "new_york", "min": 1, "max": 5, "period": {}, "basis": "base"}]}, "pay_bands[0]"),
    ({"pay_source": "glassdoor"}, "pay_source"),
    ({"posted_date": "2026-13-40"}, "posted_date"),
    ({"posted_date_source": "vibes"}, "posted_date_source"),
    ({"applicant_count": -1}, "applicant_count"),
    ({"reposted": "yes"}, "reposted"),
    ({"inbound": {"kind": "telepathy", "note": ""}}, "inbound"),
])
def test_validate_v3_problems(kw, needle):
    assert any(needle in p for p in validate(ckpt("x", **kw))), validate(ckpt("x", **kw))


def test_validate_same_role_skips_v3_facts():
    d = {"url": "u", "lead_id": "x", "batch_id": "b", "status": "complete", "checkpoint_version": 3,
         "fetch_method": "none", "company": None, "role_title": None, "location": None,
         "posting_status": "unknown", "pay_type": "unknown", "jd_read": False, "jd_url": None,
         "fit_score": None, "fit_rationale": "", "network": {"contacts": [], "summary": ""}, "engage": None,
         "airtable_match": {"relation": "same_role", "record_id": "recAAAAAAAAAAAAAA", "note": "Acme / Associate"}}
    assert validate(d) == []


def _lead(store, url="https://a.com/job/1"):
    lid, _ = store.create_or_merge(url, {**SRC, "url": url})
    return lid


def test_merge_writes_v2_lead_with_route_and_clears_v1_keys(tmp_dirs):
    s = LeadStore()
    lid = _lead(s)
    s.update(lid, "triage", {"bucket": "tier1", "bucket_reason": "strong_fit_enjoyed: x", "category": "vc",
                             "relevance": "strong", "why": "old"})
    write_ckpt(tmp_dirs, ckpt(lid))
    assert fm.merge_batch("b1", s, P) == {"merged": 1, "failed": 0, "skipped_missing": 0}
    lead = s.read(lid)
    t = lead["triage"]
    assert lead["schema_version"] == 2 and t["route"] == "board" and t["triage_status"] == "complete"
    assert t["scores"]["domain"]["score"] == 90 and t["pay_bands"][0]["min"] == 140000
    assert all(t[k] is None for k in ("bucket", "bucket_reason", "category", "relevance", "why"))


def test_merge_closed_posting_routes_to_closed(tmp_dirs):
    s = LeadStore()
    lid = _lead(s)
    write_ckpt(tmp_dirs, ckpt(lid, posting_status="closed"))
    fm.merge_batch("b1", s, P)
    assert s.read(lid)["triage"]["route"] == "closed"


def test_merge_stale_prompt_checkpoint_fails_and_keeps_version(tmp_dirs):
    s = LeadStore()
    lid = _lead(s)
    write_ckpt(tmp_dirs, ckpt(lid, checkpoint_version=2))
    assert fm.merge_batch("b1", s, P)["failed"] == 1
    lead = s.read(lid)
    assert lead["schema_version"] == 1 and "stale_prompt" in lead["triage"]["triage_error"]


def test_screen_only_and_dedupe_write_routes(tmp_dirs):
    s = LeadStore()
    a = _lead(s, "https://a.com/job/1")
    s.create_or_merge("https://a.com/job/2", {**SRC, "url": "https://a.com/job/2"}, screen={"label": "event", "reason": "meetup"})
    assert fm.screen_only(s, P) == 1
    b = [l for l in s.list() if l["lead_id"] != a][0]
    assert b["triage"]["route"] == "screened" and b["triage"]["bucket"] is None
    rec = tmp_dirs["summaries"] / "at.json"
    rec.write_text(json.dumps([{"id": "recAAAAAAAAAAAAAA", "fields": {"Role URL": "https://a.com/job/1",
                                                                      "Company": "Acme", "Role Title": "Assoc"}}]),
                   encoding="utf-8")
    assert fm.airtable_dedupe(s, rec) == 1
    assert s.read(a)["triage"]["route"] == "airtable"


def test_summary_is_an_alphabetical_run_report(tmp_dirs):
    s = LeadStore()
    for i, co in enumerate(["Zeta", "Acme"]):
        lid = _lead(s, f"https://a.com/job/{i}")
        write_ckpt(tmp_dirs, ckpt(lid, company=co, url=f"https://a.com/job/{i}"))
    fm.merge_batch("b1", s, P)
    text = fm.write_summary(s, "2026-10-13").read_text(encoding="utf-8")
    assert "Ranked board: open the Jobs tab" in text
    assert text.index("Acme") < text.index("Zeta")
    assert "## Board (2)" in text and "72 / 90" in text
    assert "Tier 1" not in text


# --- rewrites of pre-v3 tests (same intent, v3 checkpoints / v2 leads) ---

def test_merge_malformed_marks_failed(tmp_dirs):
    s = LeadStore()
    lid = _lead(s)
    write_ckpt(tmp_dirs, ckpt(lid, level="vp"))
    fm.merge_batch("b1", s, P)
    lead = s.read(lid)
    t = lead["triage"]
    assert t["triage_status"] == "failed" and t["triage_error"].startswith("malformed_checkpoint")
    assert "route" not in t and lead["schema_version"] == 1


def test_merge_failed_checkpoint(tmp_dirs):
    s = LeadStore()
    lid = _lead(s)
    write_ckpt(tmp_dirs, {"url": "u", "lead_id": lid, "batch_id": "b1", "status": "failed",
                          "fetch_method": "playwright", "error": "fetch_failed"})
    fm.merge_batch("b1", s, P)
    assert s.read(lid)["triage"]["triage_error"] == "fetch_failed"


def test_merge_is_idempotent_and_preserves_decision(tmp_dirs):
    s = LeadStore()
    lid = _lead(s)
    s.update(lid, "decision", {"decision": "skipped"})
    write_ckpt(tmp_dirs, ckpt(lid))
    fm.merge_batch("b1", s, P)
    first = {k: v for k, v in s.read(lid)["triage"].items() if k != "triaged_at"}
    fm.merge_batch("b1", s, P)
    second = {k: v for k, v in s.read(lid)["triage"].items() if k != "triaged_at"}
    assert first == second
    assert s.read(lid)["decision"]["decision"] == "skipped"
    assert s.read(lid)["schema_version"] == 2


def test_screen_only_skips_screened_out(tmp_dirs):
    s = LeadStore()
    lid = _lead(s, "https://youtu.be/x")
    s.update(lid, "intake", {"screen": {"label": "non_job", "reason": "video"}})
    assert fm.screen_only(s, P) == 1
    t = s.read(lid)["triage"]
    assert t["route"] == "screened" and t["bucket"] is None and t["bucket_reason"] == "screened out: non_job"


def test_airtable_dedupe_exact_canonical(tmp_dirs, tmp_path):
    s = LeadStore()
    lid = _lead(s)
    recs = {"records": [{"id": "recABC", "fields": {
        "Role URL": "https://a.com/job/1/?utm_source=li", "Company": "Acme", "Role Title": "Associate"}}]}
    f = tmp_path / "recs.json"
    f.write_text(json.dumps(recs), encoding="utf-8")
    assert fm.airtable_dedupe(s, f) == 1
    t = s.read(lid)["triage"]
    assert t["airtable_match"]["record_id"] == "recABC" and t["bucket"] is None and t["route"] == "airtable"


def test_airtable_dedupe_reads_configured_field_ids(tmp_dirs, tmp_path, monkeypatch):
    s = LeadStore()
    lid = _lead(s)
    monkeypatch.setattr(fm, "_airtable_fields", lambda: ("fldRoleUrl", "fldCompany", "fldRole"))
    recs = {"records": [{"id": "recDEF", "cellValuesByFieldId": {
        "fldRoleUrl": "https://a.com/job/1", "fldCompany": "Acme", "fldRole": "Associate"}}]}
    f = tmp_path / "recs.json"
    f.write_text(json.dumps(recs), encoding="utf-8")
    assert fm.airtable_dedupe(s, f) == 1 and s.read(lid)["triage"]["airtable_match"]["record_id"] == "recDEF"


def test_summary_lists_sections_with_links_and_flags(tmp_dirs):
    s = LeadStore()
    lid = _lead(s)
    write_ckpt(tmp_dirs, ckpt(lid, jd_read=False, jd_note="post only"))
    fm.merge_batch("b1", s, P)
    text = fm.write_summary(s, "2026-09-30").read_text(encoding="utf-8")
    assert "## Board (1)" in text and "Acme" in text and "https://a.com/job/1" in text
    assert "JD not read" in text and "https://www.linkedin.com/posts/pat" in text


def test_validate_never_raises_on_wrong_types():
    for kw in ({"level": ["senior"]}, {"status": []}, {"scores": "x"}, {"airtable_match": "none"},
               {"modality": {}}, {"pay_bands": "x"}):
        assert validate(ckpt("x", **kw))


def test_merge_survives_non_object_json(tmp_dirs):
    s = LeadStore()
    bad = _lead(s, "https://a.com/job/2")
    good = _lead(s)
    p = tmp_dirs["ckpt"] / "b1"
    p.mkdir(exist_ok=True)
    (p / f"{bad}.json").write_text("[1, 2]", encoding="utf-8")
    write_ckpt(tmp_dirs, ckpt(good))
    rep = fm.merge_batch("b1", s, P)
    assert rep["merged"] == 1 and rep["failed"] == 1
    assert s.read(good)["triage"]["route"] == "board"
    assert s.read(bad)["triage"]["triage_error"].startswith("malformed_checkpoint")


def test_airtable_dedupe_accepts_bare_list(tmp_dirs, tmp_path):
    s = LeadStore()
    _lead(s)
    f = tmp_path / "recs.json"
    f.write_text(json.dumps([{"id": "recX", "fields": {"Role URL": "https://a.com/job/1", "Company": "Acme",
                                                       "Role Title": "PM"}}]), encoding="utf-8")
    assert fm.airtable_dedupe(s, f) == 1


def test_summary_escapes_pipes_and_uses_readable_labels(tmp_dirs):
    s = LeadStore()
    lid = _lead(s)
    write_ckpt(tmp_dirs, ckpt(lid, company="Acme | Co", airtable_match={"relation": "reapply_candidate",
                                                                      "record_id": None, "note": ""}))
    fm.merge_batch("b1", s, P)
    text = fm.write_summary(s, "2026-09-30").read_text(encoding="utf-8")
    assert "Acme \\| Co" in text
    assert "Already in Airtable (0)" in text and "Needs attention (0)" in text
    assert "re-apply candidate" in text and "reapply candidate" not in text


GOOD_CONTACT = {"name": "Pat Example", "title": "VP", "status": "current", "warmth": "warm",
                "recommendation": "referral ask"}


def test_validate_contact_vocabulary():
    assert validate(ckpt("x", network={"contacts": [GOOD_CONTACT], "summary": "s"})) == []
    probs = validate(ckpt("x", network={"contacts": [{**GOOD_CONTACT, "status": "Current employee",
                                                       "warmth": "warm (classmate)"}], "summary": "s"}))
    assert any("contacts[0].status" in p for p in probs) and any("contacts[0].warmth" in p for p in probs)
    assert validate(ckpt("x", network={"contacts": "none", "summary": "s"}))
    assert validate(ckpt("x", network={"contacts": ["Pat"], "summary": "s"}))
    assert validate(ckpt("x", network={"contacts": [{**GOOD_CONTACT, "name": ""}], "summary": "s"}))


def test_merge_stores_network_for_the_scorer_without_bumping_route(tmp_dirs):
    # Replaces test_merge_applies_warm_contact_bump: the tier bump is gone; the warm contact is now
    # an input to the Jobs-tab scorer, so merge only has to persist it and leave the route alone.
    s = LeadStore()
    lid = _lead(s)
    write_ckpt(tmp_dirs, ckpt(lid, network={"contacts": [GOOD_CONTACT], "summary": "warm"}))
    fm.merge_batch("b1", s, P)
    t = s.read(lid)["triage"]
    assert t["route"] == "board" and t["network"]["contacts"][0]["name"] == "Pat Example"


def test_merge_ignores_the_prefetched_jd_subdir(tmp_dirs):
    """Addendum item 1: the wrapper writes <batch>/jd/<lead_id>.txt; merge reads top-level *.json only, so the
    jd/ subdir (even a stray .json in it) is never taken for a checkpoint."""
    s = LeadStore()
    lid = _lead(s)
    write_ckpt(tmp_dirs, ckpt(lid))
    jd = tmp_dirs["ckpt"] / "b1" / "jd"
    jd.mkdir()
    (jd / f"{lid}.txt").write_text("Head of Strategy\n\nOwn the roadmap.\n", encoding="utf-8")
    (jd / "deadbeef0000.json").write_text("{not a checkpoint", encoding="utf-8")
    assert fm.merge_batch("b1", s, P) == {"merged": 1, "failed": 0, "skipped_missing": 0}
    assert s.read(lid)["triage"]["triage_status"] == "complete"
