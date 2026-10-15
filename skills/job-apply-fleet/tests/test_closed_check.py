import hashlib
import json

import pytest

import closed_check as cc
from lib.lead_store import LeadStore


def _mk(s, n, **dec):
    url = f"https://a.com/job/{n}"
    lid, _ = s.create_or_merge(url, {"url": url, "kind": "posting"})
    s.update(lid, "triage", {"triage_status": "complete", "company": f"Co{n}", "role_title": "Role", "jd_url": url,
                             "bucket": "tier2"})
    if dec:
        s.update(lid, "decision", dec)
    return lid


LD = '<script type="application/ld+json">{"@type": "JobPosting", "title": "X", "validThrough": "%s"}</script>'


@pytest.mark.parametrize("status,text,v", [
    (404, "", "closed"), (410, "", "closed"),
    (200, LD % "2026-09-01", "closed"),                                           # structured: expired
    (200, LD % "2026-12-31" + "<footer>Some roles are no longer accepting applications</footer>", "open"),
    (200, "<main><h1>No longer accepting applications</h1></main>", "closed"),
    (200, "<main>Sorry, this job is no longer available</main>", "closed"),
    (200, "<main><button>Apply now</button></main><footer>job not found? contact us</footer>", "open"),
    (200, "<nav>position has been filled</nav><main>Great role</main>", "unknown"),   # phrase only outside main
    (200, "<div id=root></div>", "unknown"),
    (None, "", "unknown"),
])
def test_verdict(status, text, v):
    assert cc.verdict(status, text, today="2026-10-14")[0] == v


def test_eligible_excludes_applied_in_progress_pending_airtable_screened_but_keeps_v1_skips(tmp_dirs):
    s = LeadStore()
    ok = _mk(s, 1)
    applied = _mk(s, 2, decision="applied")
    helping = _mk(s, 3, decision="needs_help")
    pend = _mk(s, 4, pending={"action": "skip"})
    at = _mk(s, 5)
    s.update(at, "triage", {"airtable_match": {"relation": "same_role"}})
    v1skip = _mk(s, 6)
    s.update(v1skip, "triage", {"bucket": "skip"})                 # v1 auto-skip (unpaid / closed): checked again
    scr = _mk(s, 7)
    s.update(scr, "triage", {"route": "screened"})
    got = {l["lead_id"] for l in s.list() if cc.eligible(l)}
    assert got == {ok, v1skip}


def test_run_writes_report_and_never_touches_leads(tmp_dirs):
    s = LeadStore()
    a, b, c = _mk(s, 1), _mk(s, 2), _mk(s, 3)
    before = {p.name: hashlib.sha256(p.read_bytes()).hexdigest() for p in tmp_dirs["leads"].iterdir()}
    pages = {"https://a.com/job/1": (404, ""), "https://a.com/job/2": (200, "Apply now"), "https://a.com/job/3": (200, "")}
    out = cc.run(s, "2026-10-14", fetcher=lambda u, timeout=25: (u, *pages[u]))
    after = {p.name: hashlib.sha256(p.read_bytes()).hexdigest() for p in tmp_dirs["leads"].iterdir()}
    assert before == after
    data = json.loads(out.with_suffix(".json").read_text(encoding="utf-8"))
    assert {r["lead_id"]: r["verdict"] for r in data["results"]} == {a: "closed", b: "open", c: "unknown"}
    md = out.read_text(encoding="utf-8")
    assert "## Closed (1)" in md and "## Unknown — needs a look (1)" in md and "Nothing has been removed" in md


def test_closed_apply_posts_only_confirmed_closed_ids_with_closed_stale():
    import closed_apply
    report = {"results": [{"lead_id": "aaaaaaaaaaaa", "verdict": "closed"}, {"lead_id": "bbbbbbbbbbbb", "verdict": "open"}]}
    calls = []
    res = closed_apply.apply(["aaaaaaaaaaaa", "bbbbbbbbbbbb", "cccccccccccc"], report,
                             lambda p, b: (calls.append((p, b)) or (200, {})), "2026-10-14")
    assert calls == [("/api/jobs/leads/aaaaaaaaaaaa/action",
                      {"action": "skip", "reason": "closed_stale", "note": "closed-check 2026-10-14 (user confirmed)"})]
    assert res == {"posted": ["aaaaaaaaaaaa"], "refused": ["bbbbbbbbbbbb", "cccccccccccc"], "failed": []}
