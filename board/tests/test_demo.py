"""Demo mode (python -m copilot demo): Applied and Prep run locally, never through the claude CLI; plain serve keeps
the buttons tied to the job_apply module."""
import importlib.util
import json
import sys
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

BACKEND = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BACKEND))

import demo  # noqa: E402
import jobs  # noqa: E402

T0 = datetime(2026, 10, 15, 12, 0, tzinfo=timezone.utc)
SHOWN = {"score": 80, "rank": 1, "band": "apply_now"}


def _record_id():
    spec = importlib.util.spec_from_file_location("record_id", jobs.REPO_ROOT / "scripts" / "record_id.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


class Clock:
    def __init__(self):
        self.now = T0

    def __call__(self):
        return self.now


def no_dispatch(*a, **k):
    raise AssertionError("demo mode must never dispatch")


def make(tmp_path, demo_mode=True, dispatch=False):
    repo = tmp_path / "repo"
    leads = repo / "data" / "leads"
    leads.mkdir(parents=True)
    clock = Clock()
    svc = jobs.JobsService(repo_root=repo, leads_dir=leads, clock=clock, create_job=no_dispatch,
                           run_extractor=no_dispatch, spawn=no_dispatch, prepare_lockdown=no_dispatch,
                           dispatch_enabled=dispatch, demo=demo_mode)
    url = "https://jobs.example.com/acme/1"
    lid, _ = svc.store.create_or_merge(url, {"url": url, "kind": "posting", "poster_name": None, "poster_url": None,
                                             "added_via": "manual", "roundup_of": None, "hint": None})
    svc.store.update(lid, "triage", {
        "triage_status": "complete", "company": "Acme Grid", "role_title": "Product Manager", "route": "board",
        "location": "Boston, MA", "jd_url": url, "fit_score": "High", "fit_rationale": "Strong fit.",
        "job_summary": "Own the forecasting product", "main_gap": "No utility customers",
        "scores": {"role_fit": {"score": 80, "evidence": "Shipped two data products"},
                   "domain": {"score": 90, "evidence": "Grid software"}}}, schema_version=2)
    app = FastAPI()
    app.include_router(jobs.make_router(svc))
    return svc, TestClient(app), lid, repo, clock


def run(svc, client, clock, lid, action):
    r = client.post(f"/api/jobs/leads/{lid}/action", json={"action": action, "shown": SHOWN})
    assert r.status_code == 200, r.text
    clock.now += timedelta(seconds=91)          # past the undo window
    svc.sweep()
    return svc.store.read(lid)["decision"]


def test_demo_mode_is_exposed_on_the_lead_list_and_health(tmp_path, monkeypatch):
    svc, client, *_ = make(tmp_path)
    body = client.get("/api/jobs/leads").json()
    assert body["demo"] is True and body["dispatch_enabled"] is False
    import server
    monkeypatch.setattr(server, "JOBS", svc)
    assert TestClient(server.app).get("/api/health").json()["demo"] is True
    built, _ = server.build(leads_dir=tmp_path / "L", config_path=tmp_path / "c.json", demo=True)
    assert built.demo is True


def test_demo_applied_writes_a_uuid5_local_record_and_never_dispatches(tmp_path):
    svc, client, lid, repo, clock = make(tmp_path)
    d = run(svc, client, clock, lid, "applied")
    rid = _record_id().local_record_id(lead_id=lid)
    assert d["decision"] == "applied" and d["airtable_record_id"] == rid
    assert d["job_status"]["state"] == "done" and d["job_status"]["record_id"] == rid
    assert uuid.UUID(rid).version == 5 and jobs.RECORD_ID_RE.fullmatch(rid)
    lines = (repo / "data" / "applications.jsonl").read_text(encoding="utf-8").splitlines()
    assert len(lines) == 1
    rec = json.loads(lines[0])
    assert rec["record_id"] == rid and rec["company"] == "Acme Grid" and rec["status"] == "Applied"
    demo.write_local_record(repo / "data", svc.store.read(lid), clock.now)       # a re-run adds no second line
    assert len((repo / "data" / "applications.jsonl").read_text(encoding="utf-8").splitlines()) == 1


def test_demo_prep_writes_a_labeled_sample_pack_the_card_can_open(tmp_path):
    svc, client, lid, repo, clock = make(tmp_path)
    d = run(svc, client, clock, lid, "prep")
    assert d["decision"] == "needs_help" and d["job_status"] == {**d["job_status"], "kind": "prep", "state": "done"}
    assert d["prep_file"] == "data/prep/acme-grid-2026-10-15.md"
    text = (repo / d["prep_file"]).read_text(encoding="utf-8")
    assert demo.DEMO_LABEL in text and "Why are you interested in Acme Grid?" in text
    assert "No utility customers" in text
    assert "Acme Grid is working on the problem this role exists for: own the forecasting product." in text
    assert "For this role that means own the forecasting product." in text
    notes = text.split("## Drafting notes (from triage)")[1].split("## Open questions")[0]
    assert "- Role fit: Shipped two data products" in notes and "- Domain: Grid software" in notes
    answers = text.split("## Answers")[1].split("## Drafting notes")[0]
    assert "Shipped two data products" not in answers and "Grid software" not in answers
    assert demo.prep_pack_text(svc.store.read(lid), clock.now) == text                # deterministic


def test_serve_with_job_apply_off_still_hides_the_buttons(tmp_path):
    svc, client, lid, *_ = make(tmp_path, demo_mode=False, dispatch=False)
    body = client.get("/api/jobs/leads").json()
    assert body["demo"] is False and body["dispatch_enabled"] is False
    for a in ("applied", "prep"):
        r = client.post(f"/api/jobs/leads/{lid}/action", json={"action": a, "shown": SHOWN})
        assert r.status_code == 503 and "job_apply module is off" in r.json()["detail"]


def test_default_build_is_not_demo(monkeypatch, tmp_path):
    import server
    monkeypatch.delenv("JSC_DEMO", raising=False)
    built, _ = server.build(leads_dir=tmp_path / "L", config_path=tmp_path / "c.json")
    assert built.demo is False


@pytest.mark.parametrize("env,expected", [("1", True), ("0", False)])
def test_copilot_demo_sets_the_flag_through_the_environment(monkeypatch, tmp_path, env, expected):
    import server
    monkeypatch.setenv("JSC_DEMO", env)
    built, _ = server.build(leads_dir=tmp_path / "L", config_path=tmp_path / "c.json")
    assert built.demo is expected
