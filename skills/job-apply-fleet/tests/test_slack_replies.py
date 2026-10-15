from lib import slack_replies as sr
from lib.slack_intake import FINAL_TOKEN

LABELS = ["event", "non_job", "internship_or_undergrad"]


def lead(lid, **t):
    intake = t.pop("intake", {})
    return {"lead_id": lid, "canonical_url": f"https://x.com/{lid}", "intake": intake,
            "triage": {"triage_status": "complete", "route": "board", "company": "Acme",
                       "role_title": "Head of Strategy", "location": "NYC", "jd_read": True, **t}}


def ctx(leads, scores=None, run_ok=True, airtable_skipped=False, pre=()):
    return sr.Ctx(leads={l["lead_id"]: l for l in leads}, scores=scores, run_ok=run_ok,
                  airtable_skipped=airtable_skipped, screen_labels=LABELS, pre_triaged=set(pre))


def one(lid):
    return {"status": "ok", "lead_ids": [lid], "roundup": False}


def test_board_line_with_score_and_final_marker():
    c = ctx([lead("a")], scores={"a": 82})
    text, final, pending = sr.render({"u": one("a")}, c)
    assert final and pending == []
    assert text.splitlines()[0].endswith(FINAL_TOKEN)
    assert "✅ Acme · Head of Strategy · NYC · score 82" in text


def test_every_kind_has_its_line():
    c = ctx([lead("b", route="closed"), lead("c", jd_read=False),
             lead("d", triage_status="pending", route=None, intake={"screen": {"label": "event", "reason": "AI summit page"}}),
             lead("e", route="airtable"), lead("f"), lead("g", triage_status="failed", triage_error="timeout")],
            scores={"f": 71}, pre=["f"])
    assert sr.classify(c.leads["b"], c)[1] == "🔒 Acme · Head of Strategy · closed posting"
    assert sr.classify(c.leads["c"], c)[0] == "unread"
    assert sr.classify(c.leads["d"], c)[1] == '🚫 screened out: event — "AI summit page"'
    assert sr.classify(c.leads["e"], c)[0] == "airtable"
    assert sr.classify(c.leads["f"], c)[1] == "↩️ already on board: Acme · Head of Strategy · score 71"
    assert sr.classify(c.leads["g"], c)[1] == ("⚠️ Acme · Head of Strategy — Could not parse job description "
                                               "(timeout). Re-drop with a cleaner link or run directly via /job-apply.")


def test_pending_is_queued_when_run_ok_else_unfinished_and_reply_is_partial():
    p = lead("p", triage_status="pending", route=None, company=None, role_title=None)
    text, final, pending = sr.render({"u": one("p")}, ctx([p], run_ok=True))
    assert not final and pending == ["p"] and "queued (over tonight's cap)" in text
    assert FINAL_TOKEN not in text
    text2, _, _ = sr.render({"u": one("p")}, ctx([p], run_ok=False))
    assert "triage didn't finish, retrying next run" in text2


def test_failed_and_skipped_urls_and_no_link():
    by_url = {"u1": {"status": "failed", "lead_ids": [], "roundup": False, "error": "RuntimeError: boom"},
              "u2": {"status": "previously_skipped", "lead_ids": [], "roundup": False}}
    text, final, _ = sr.render(by_url, ctx([]))
    assert "⚠️ Could not parse job description (RuntimeError: boom)." in text and "⏭️ skipped earlier on the Jobs tab, not re-added" in text
    assert final
    assert sr.render({}, ctx([])) == (f"No link found in this message. {FINAL_TOKEN}", True, [])


def test_roundup_line_counts_and_top():
    leads = [lead("r1", company="Eps", role_title="Strategy Lead"), lead("r2", company="Zed"),
             lead("r3", triage_status="pending", route="screened", intake={"screen": {"label": "event"}}),
             lead("r4", triage_status="pending", route=None)]
    by_url = {"post": {"status": "ok", "lead_ids": ["r1", "r2", "r3", "r4"], "roundup": True}}
    text, final, pending = sr.render(by_url, ctx(leads, scores={"r1": 77, "r2": 60}))
    assert "📋 roundup (4 roles): 2 on board — top: Eps · Strategy Lead (score 77); 1 screened; 1 queued" in text
    assert not final and pending == ["r4"]


def test_only_renders_carried_entries():
    by_url = {"u1": one("a"), "u2": one("b")}
    text, final, _ = sr.render(by_url, ctx([lead("a", company="A1"), lead("b", company="B1")]), only=["b"])
    assert "B1" in text and "A1" not in text and final


def test_footers_and_line_cap():
    leads = [lead(f"x{i}") for i in range(20)]
    by_url = {f"u{i}": one(f"x{i}") for i in range(20)}
    text, _, _ = sr.render(by_url, ctx(leads, scores=None, airtable_skipped=True))
    lines = text.splitlines()
    assert len(lines) <= sr.MAX_LINES
    assert "Full list on the Jobs board." in text
    assert lines[-2:] == ["Airtable dedupe skipped tonight.", "Scores on the board (scorer unavailable)."]


def test_missing_lead_file_is_a_failed_line():
    text, final, _ = sr.render({"u": one("gone")}, ctx([]))
    assert "❌ lead file missing" in text and final


def test_closed_or_failed_redrop_shows_its_real_state_not_already_on_board():
    """Fix I: the pre_triaged check runs after closed and failed."""
    c = ctx([lead("b", route="closed"), lead("g", triage_status="failed", triage_error="timeout")], pre=["b", "g"])
    assert sr.classify(c.leads["b"], c)[0] == "closed"
    assert sr.classify(c.leads["g"], c)[0] == "failed"


def test_failed_lead_due_a_retry_reads_pending():
    """Fix D: a re-dropped failed lead is pending until it is triaged again."""
    g = lead("g", triage_status="failed", triage_error="timeout")
    c = ctx([g])
    c.retry = {"g"}
    text, final, pending = sr.render({"u": one("g")}, c)
    assert not final and pending == ["g"] and "queued" in text and "❌" not in text
    c.run_ok = False
    assert sr.classify(g, c)[0] == "unfinished"


# ---- addendum item 2: failed-scrape wording ------------------------------------------------------------

REDROP = "Re-drop with a cleaner link or run directly via /job-apply."


def test_failed_lead_with_its_automatic_retry_left_is_pending_and_says_so():
    g = lead("g", triage_status="failed", triage_error="fetch_failed")
    c = ctx([g])
    c.auto_retry = {"g"}
    text, final, pending = sr.render({"u": one("g")}, c)
    assert not final and pending == ["g"] and FINAL_TOKEN not in text
    assert "⏳ Acme · Head of Strategy — couldn't read it tonight (fetch_failed); retrying once tomorrow night" in text


def test_failed_lead_without_a_retry_left_gets_the_final_wording():
    g = lead("g", triage_status="failed", triage_error="x" * 200)
    text, final, pending = sr.render({"u": one("g")}, ctx([g]))
    assert final and pending == []
    assert f"⚠️ Acme · Head of Strategy — Could not parse job description ({'x' * 80}). {REDROP}" in text
    assert "x" * 81 not in text


def test_ingest_failure_uses_the_same_wording_without_a_name():
    by_url = {"u1": {"status": "failed", "lead_ids": [], "roundup": False, "error": "RuntimeError: boom"}}
    text, final, _ = sr.render(by_url, ctx([]))
    assert final and f"⚠️ Could not parse job description (RuntimeError: boom). {REDROP}" in text


def test_roundup_counts_retrying_apart_from_queued():
    leads = [lead("r1"), lead("r2", triage_status="failed", triage_error="t"),
             lead("r3", triage_status="failed", triage_error="t"), lead("r4", triage_status="pending", route=None)]
    c = ctx(leads)
    c.auto_retry = {"r2"}
    by_url = {"post": {"status": "ok", "lead_ids": ["r1", "r2", "r3", "r4"], "roundup": True}}
    text, final, pending = sr.render(by_url, c)
    assert "1 on board; 1 queued; 1 retrying tomorrow; 1 failed" in text
    assert not final and sorted(pending) == ["r2", "r4"]
