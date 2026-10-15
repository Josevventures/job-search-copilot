"""Regenerate samples/leads/*.json: 20 invented job leads for the demo board.

Every company, person, URL and number below is made up. The leads go through the real pipeline code: intake
creates each lead file (lead_store), then each invented triage result is written as a checkpoint and merged by
fleet_merge, exactly as an AI triage subagent's output would be. So the samples always match the live schema.

    python samples/make_samples.py

Dates are anchored to ANCHOR; `python -m copilot demo` shifts them so the demo board is always "today".
"""
import json
import os
import shutil
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SKILL = ROOT / "skills" / "job-apply-fleet"
OUT = ROOT / "samples" / "leads"
ANCHOR = "2026-10-15"

T = "2026-10-15T09:00:00+00:00"


def place(label, metro):
    return {"label": label, "metro": metro}


def band(metro, lo, hi, basis="base", period="year"):
    return {"location_metro": metro, "min": lo, "max": hi, "period": period, "basis": basis}


def contact(name, title, status, warmth, rec):
    return {"name": name, "title": title, "status": status, "warmth": warmth, "recommendation": rec}


# (canonical url, source kind, triage facts). Persona: the invented candidate in preferences.example.md
# (product manager, climate / developer-tools direction, home metro Boston).
LEADS = [
    ("https://job-boards.greenhouse.io/saltmarshenergy/jobs/4100001", dict(
        company="Saltmarsh Energy", role_title="Senior Product Manager, Grid Analytics", level="senior",
        job_summary="Own the forecasting product used by utility grid operators",
        locations=[place("Boston, MA", "boston")], modality="hybrid",
        pay_bands=[band("boston", 135000, 165000)], pay_source="posting", bonus=True, equity=True,
        posted_date="2026-10-13", applicant_count=34,
        scores={"role_fit": {"score": 84, "evidence": "Five years of B2B product work, two on energy data"},
                "domain": {"score": 93, "evidence": "Grid software is the center of the stated direction"}},
        main_gap="No direct utility-customer experience on record",
        network={"contacts": [contact("Dana Okafor", "Director of Product, Saltmarsh Energy", "current", "warm",
                                      "Referral ask: former teammate, replied last month")], "summary": "One warm current contact."},
        fit_score="High")),
    ("https://jobs.lever.co/brambleworks/00000000-0000-4000-8000-000000000002", dict(
        company="Brambleworks", role_title="Product Manager, Developer Platform", level="mid",
        job_summary="Run the CLI and SDK roadmap for a build-tooling company",
        locations=[place("Remote (US)", "remote_us")], modality="remote",
        pay_bands=[band("remote_us", 128000, 150000)], pay_source="posting", equity=True,
        posted_date="2026-10-09", applicant_count=180,
        scores={"role_fit": {"score": 76, "evidence": "Shipped two developer-facing APIs; no CLI product yet"},
                "domain": {"score": 78, "evidence": "Developer tools, second tier of the direction"}},
        main_gap="Never owned a command-line product", fit_score="Medium")),
    ("https://jobs.ashbyhq.com/kestrelledger/00000000-0000-4000-8000-000000000003", dict(
        company="Kestrel Ledger", role_title="Product Lead, Carbon Accounting", level="manager",
        job_summary="Lead a three-PM team building emissions reporting for mid-size companies",
        locations=[place("New York, NY", "new_york"), place("Boston, MA", "boston")], modality="hybrid",
        pay_bands=[band("new_york", 165000, 195000), band("boston", 155000, 185000)], pay_source="ats_api",
        bonus=None, equity=True, posted_date="2026-10-02", applicant_count=None,
        inbound={"kind": "recruiter_outreach", "note": "Recruiter messaged after the candidate's post on reporting tools"},
        scores={"role_fit": {"score": 62, "evidence": "Strong IC record; has not managed PMs"},
                "domain": {"score": 90, "evidence": "Climate software with a clear product surface"}},
        main_gap="No people-management title", fit_score="Medium")),
    ("https://careers.orchardline.example/jobs/pm-data-products", dict(
        company="Orchardline", role_title="Product Manager, Data Products", level="mid",
        job_summary="Turn an internal pricing dataset into a paid API",
        locations=[place("Chicago, IL", "chicago")], modality="onsite", relocation_assistance=True,
        pay_bands=[], pay_source="none", posted_date=None, applicant_count=None,
        scores={"role_fit": {"score": 80, "evidence": "Has launched one paid data API end to end"},
                "domain": {"score": 72, "evidence": "B2B data product, outside climate"}},
        main_gap="Retail pricing domain is new", fit_score="Medium")),
    ("https://job-boards.greenhouse.io/vellumatlas/jobs/4100005", dict(
        company="Vellum Atlas", role_title="Senior PM, Mapping Infrastructure", level="senior",
        job_summary="Own the map-tile pipeline that partners build on",
        locations=[place("Seattle, WA", "seattle"), place("Remote (US)", "remote_us")], modality="remote",
        pay_bands=[band("remote_us", 150000, 182000)], pay_source="posting", bonus=True,
        posted_date="2026-09-20", reposted=True, applicant_count=420,
        scores={"role_fit": {"score": 70, "evidence": "Platform PM work; geospatial is new"},
                "domain": {"score": 74, "evidence": "Data platform for developers"}},
        main_gap="No geospatial background", fit_score="Medium")),
    ("https://jobs.ashbyhq.com/tallgrasspay/00000000-0000-4000-8000-000000000006", dict(
        company="Tallgrass Payments", role_title="Product Manager, Risk", level="mid",
        job_summary="Build the fraud-review queue for small-business payments",
        locations=[place("Austin, TX", "austin")], modality="hybrid",
        pay_bands=[band("austin", 92000, 118000)], pay_source="posting", equity=True,
        posted_date="2026-10-11", applicant_count=61,
        scores={"role_fit": {"score": 74, "evidence": "Ran an internal review tool; no payments work"},
                "domain": {"score": 45, "evidence": "Fintech operations, outside the direction"}},
        main_gap="No payments or risk background", fit_score="Low")),
    ("https://jobs.lever.co/lanternfish/00000000-0000-4000-8000-000000000007", dict(
        company="Lanternfish AI", role_title="Founding Product Manager", level="senior",
        job_summary="First PM at a seed-stage company building agents for field technicians",
        locations=[place("Boston, MA", "boston")], modality="onsite",
        pay_type="salary", pay_bands=[band("boston", 85000, 98000)], pay_source="posting", equity=True,
        posted_date="2026-10-12", applicant_count=12,
        inbound={"kind": "hiring_manager_post", "note": "Founder posted the role and invited applications"},
        scores={"role_fit": {"score": 81, "evidence": "Zero-to-one launches at two startups"},
                "domain": {"score": 70, "evidence": "Applied AI for industrial work, adjacent to energy"}},
        main_gap=None, fit_score="High")),
    ("https://careers.mossgrove.example/openings/product-ops", dict(
        company="Mossgrove Climate", role_title="Product Operations Manager", level="manager",
        job_summary="Run release planning and customer-feedback intake for a climate-data team",
        locations=[place("Boston, MA", "boston")], modality="hybrid",
        pay_bands=[], pay_source="none", posted_date="2026-08-30", posted_date_source="linkedin_relative",
        applicant_count=95,
        network={"contacts": [contact("Lee Marchetti", "Data Scientist, Mossgrove Climate", "current", "cold",
                                      "Met once at a meetup; a short note before applying")], "summary": "One cold current contact."},
        scores={"role_fit": {"score": 68, "evidence": "Has run release trains; title would be a step sideways"},
                "domain": {"score": 88, "evidence": "Climate data company"}},
        main_gap="Operations title rather than product", fit_score="Medium")),
    ("https://job-boards.greenhouse.io/quarrynine/jobs/4100009", dict(
        company="Quarry Nine", role_title="Staff Product Manager, Billing", level="director_plus",
        job_summary="Re-platform usage-based billing for an infrastructure company",
        locations=[place("San Francisco, CA", "san_francisco")], modality="onsite",
        pay_bands=[band("san_francisco", 210000, 250000)], pay_source="posting", bonus=True, equity=True,
        posted_date="2026-10-07", applicant_count=210,
        scores={"role_fit": {"score": 38, "evidence": "Staff level asks for 10+ years; record shows about 6"},
                "domain": {"score": 62, "evidence": "Infrastructure tooling, billing focus"}},
        main_gap="Years of experience well short of the stated bar", fit_score="Low")),
    ("https://jobs.ashbyhq.com/harborlight/00000000-0000-4000-8000-000000000010", dict(
        company="Harborlight Health", role_title="Product Manager, Clinician Tools", level="mid",
        job_summary="Improve scheduling software used by home-care nurses",
        locations=[place("Providence, RI", "other_us")], modality="hybrid",
        pay_bands=[band("other_us", 110000, 130000)], pay_source="posting",
        posted_date="2026-10-01", applicant_count=48,
        scores={"role_fit": {"score": 72, "evidence": "Scheduling and workflow products before"},
                "domain": {"score": 40, "evidence": "Healthcare operations, not in the direction"}},
        main_gap="No healthcare experience", fit_score="Low")),
    ("https://jobs.lever.co/ironbark/00000000-0000-4000-8000-000000000011", dict(
        company="Ironbark Data", role_title="Product Manager, Observability", level="mid",
        job_summary="Own alerting and dashboards for a metrics database",
        locations=[place("Remote (US)", "remote_us")], modality="remote",
        pay_type="unknown", pay_bands=[], pay_source="none", posted_date="2026-10-14", applicant_count=None,
        scores={"role_fit": {"score": 79, "evidence": "Built internal monitoring dashboards as a PM"},
                "domain": {"score": 76, "evidence": "Developer infrastructure"}},
        main_gap="Has not sold to SRE buyers", fit_score="Medium")),
    ("https://careers.pinecrest.example/jobs/senior-pm-charging", dict(
        company="Pinecrest Mobility", role_title="Senior PM, EV Charging Network", level="senior",
        job_summary="Own uptime and pricing for a public charging network",
        locations=[place("Washington, DC", "washington_dc"), place("Boston, MA", "boston")], modality="hybrid",
        pay_bands=[band("washington_dc", 140000, 170000)], pay_source="posting", bonus=True,
        posted_date="2026-10-10", applicant_count=150,
        network={"contacts": [contact("Robin Hale", "Former Head of Ops, Pinecrest Mobility", "former", "warm",
                                      "Ask for a read on the team before applying")], "summary": "One warm former contact."},
        scores={"role_fit": {"score": 77, "evidence": "Hardware-adjacent software PM; pricing experiments shipped"},
                "domain": {"score": 86, "evidence": "Electrification, core direction"}},
        main_gap="No charging-hardware exposure", fit_score="Medium")),
    ("https://jobs.ashbyhq.com/driftwood/00000000-0000-4000-8000-000000000013", dict(
        company="Driftwood Analytics", role_title="Product Manager (Contract, 6 months)", level="mid",
        job_summary="Scope a reporting module for a supply-chain analytics tool",
        locations=[place("Remote (US)", "remote_us")], modality="remote",
        pay_bands=[band("remote_us", 70, 85, period="hour")], pay_source="posting",
        posted_date="2026-10-08", applicant_count=75,
        scores={"role_fit": {"score": 82, "evidence": "Reporting products are a strength"},
                "domain": {"score": 55, "evidence": "Supply-chain analytics, adjacent"}},
        main_gap="Contract role; no benefits stated", fit_score="Medium")),
    ("https://job-boards.greenhouse.io/emberoak/jobs/4100014", dict(
        company="Ember & Oak Software", role_title="Associate Product Manager", level="entry",
        job_summary="Rotational APM program for recent graduates",
        locations=[place("Boston, MA", "boston")], modality="onsite",
        pay_bands=[band("boston", 88000, 96000)], pay_source="posting",
        posted_date="2026-10-06", applicant_count=500,
        eligibility_note="Program is for 2025-2026 graduates; the candidate graduated in 2024",
        scores={"role_fit": {"score": 18, "evidence": "Outside the program's graduation window"},
                "domain": {"score": 60, "evidence": "General software"}},
        main_gap="Graduation window", fit_score="Low")),
    ("https://jobs.lever.co/cobaltmeadow/00000000-0000-4000-8000-000000000015", dict(
        company="Cobalt Meadow", role_title="Product Manager, Battery Analytics", level="mid",
        job_summary="Build state-of-health analytics for grid batteries",
        locations=[place("Boston, MA", "boston")], modality="hybrid",
        pay_type="equity_only", pay_bands=[], pay_source="posting", equity=True,
        posted_date="2026-10-13", applicant_count=9,
        scores={"role_fit": {"score": 80, "evidence": "Analytics PM; battery data is new"},
                "domain": {"score": 95, "evidence": "Grid storage, center of the direction"}},
        main_gap="Equity-only offer", fit_score="Medium")),
    ("https://careers.bluefinch.example/jobs/pm-fleet", dict(
        company="Bluefinch Robotics", role_title="Product Manager, Fleet Software", level="mid",
        job_summary="Own the dashboard operators use to supervise warehouse robots",
        locations=[place("Boston, MA", "boston")], modality="onsite",
        pay_bands=[band("boston", 120000, 145000)], pay_source="posting", equity=True,
        posted_date="2026-09-01", applicant_count=None, posting_status="closed",
        scores={"role_fit": {"score": 75, "evidence": "Operator-facing dashboards shipped before"},
                "domain": {"score": 66, "evidence": "Robotics software, adjacent"}},
        main_gap="No robotics background", fit_score="Medium")),
]
FAILED = ("https://careers.wrensystems.example/jobs/12345", "fetch_failed")
V1 = ("https://jobs.lever.co/quillmate/00000000-0000-4000-8000-000000000018",
      dict(company="Quillmate", role_title="Product Manager, Editor", location="Remote (US)", pay=None,
           fit_score="Medium", fit_rationale="Older triage format: no sub-scores, so it waits for re-triage.",
           bucket="tier2", bucket_reason="v1 rule table", category="product", relevance="medium"))
POST_ROUNDUP = "https://www.linkedin.com/posts/example-recruiter_three-climate-pm-roles-share-0000000000000000000"


def checkpoint(lid, url, batch, facts):
    t = {"url": url, "lead_id": lid, "batch_id": batch, "status": "complete", "checkpoint_version": 3,
         "fetch_method": "api" if "ashbyhq" in url or "greenhouse" in url else "webfetch",
         "fetched_at": T, "location": None, "pay": None, "pay_type": "salary", "jd_url": url, "jd_read": True,
         "jd_note": None, "posting_status": "open", "fit_rationale": "", "network": {"contacts": [], "summary": ""},
         "engage": None, "airtable_match": {"relation": "none", "record_id": None, "note": None, "stage": None},
         "eligibility_note": None, "alternates": [], "draft_answers": None, "main_gap": None,
         "relocation_assistance": None, "bonus": None, "equity": None, "posted_date_source": "posting",
         "reposted": False, "applicant_count": None, "inbound": None, "pay_bands": [], "pay_source": "none"}
    t.update(facts)
    if t["posted_date"] is None:
        t["posted_date_source"] = "none"
    t["location"] = t["location"] or " / ".join(l["label"] for l in t["locations"]) + (
        f" ({t['modality']})" if t.get("modality") not in (None, "unknown") else "")
    if t["pay_bands"]:
        b = t["pay_bands"][0]
        unit = "/hr" if b["period"] == "hour" else ""
        t["pay"] = f"${b['min']:,}{unit} - ${b['max']:,}{unit}"
    if not t["fit_rationale"]:
        t["fit_rationale"] = f"{t['scores']['role_fit']['evidence']}. Main gap: {t['main_gap'] or 'none named'}."
    return t


def main():
    work = Path(tempfile.mkdtemp(prefix="jsc-samples-"))
    os.environ["JOB_LEADS_DIR"] = str(work / "leads")
    os.environ["JOB_CHECKPOINTS_DIR"] = str(work / "ckpt")
    sys.path.insert(0, str(SKILL))
    from lib.lead_store import LeadStore
    from lib.prefs import load
    import fleet_merge

    store, batch = LeadStore(), "sample-batch"
    (work / "ckpt" / batch).mkdir(parents=True)
    src = lambda url, kind="posting", **kw: {"url": url, "kind": kind, "poster_name": kw.get("poster"),  # noqa: E731
                                            "poster_url": kw.get("roundup"), "added_via": kw.get("via", "manual"),
                                            "roundup_of": kw.get("roundup"), "hint": kw.get("hint"),
                                            "added_at": T, **({"note": kw["note"]} if kw.get("note") else {})}
    for i, (url, facts) in enumerate(LEADS):
        via = "slack" if i % 3 == 0 else "manual"
        note = "Saw the founder's post; ask about the battery pilot" if facts["company"] == "Lanternfish AI" else None
        roundup = POST_ROUNDUP if facts["company"] in ("Mossgrove Climate", "Pinecrest Mobility") else None
        lid, _ = store.create_or_merge(url, src(url, via=via, note=note, roundup=roundup,
                                                poster="Example Recruiter" if roundup else None),
                                       jd_links=[url])
        cp = checkpoint(lid, url, batch, facts)
        (work / "ckpt" / batch / f"{lid}.json").write_text(json.dumps(cp), encoding="utf-8")
    lid, _ = store.create_or_merge(FAILED[0], src(FAILED[0]), jd_links=[FAILED[0]])
    (work / "ckpt" / batch / f"{lid}.json").write_text(json.dumps(
        {"url": FAILED[0], "lead_id": lid, "batch_id": batch, "status": "failed", "fetch_method": "webfetch",
         "error": FAILED[1]}), encoding="utf-8")
    print(fleet_merge.merge_batch(batch, store, load()))
    # One lead triaged under the old (v1) schema: no sub-scores, so the board shows it under Needs re-triage.
    lid, _ = store.create_or_merge(V1[0], src(V1[0]), jd_links=[V1[0]])
    store.update(lid, "triage", {"triage_status": "complete", "triaged_at": T, "batch_id": "v1-sample",
                                 "jd_url": V1[0], "jd_read": True, **V1[1]})
    # One link that intake created but triage has not seen yet.
    pending = "https://jobs.ashbyhq.com/juniperrelay/00000000-0000-4000-8000-000000000020"
    store.create_or_merge(pending, src(pending, via="slack", note="Looks like the grid role from the newsletter"),
                          jd_links=[pending])

    if OUT.exists():
        shutil.rmtree(OUT)
    OUT.mkdir(parents=True)
    n = 0
    for lead in store.list():
        lead["intake"]["created_at"] = T
        lead["triage"]["triaged_at"] = T if lead["triage"].get("triaged_at") else None
        if lead["triage"].get("triage_status") == "pending":
            lead["triage"].pop("triaged_at", None)
        (OUT / f"{lead['lead_id']}.json").write_text(json.dumps(lead, ensure_ascii=False, indent=2) + "\n",
                                                    encoding="utf-8", newline="\n")
        n += 1
    shutil.rmtree(work)
    print(f"wrote {n} leads to {OUT.relative_to(ROOT)} (anchor {ANCHOR})")


if __name__ == "__main__":
    main()
