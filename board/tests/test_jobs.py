"""Tests for jobs.py — the Jobs tab backend (§4, §8, §9). The clock and dispatch functions are injected: no sleeps, no real claude -p.

Run from the repo root:  python -m pytest board/tests -q
"""
import json
import re
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

BACKEND = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BACKEND))

import jobs  # noqa: E402
import prep_lockdown  # noqa: E402
from prep_extract import schema  # noqa: E402  (stdlib-only module)


class Clock:
    def __init__(self):
        self.t = datetime(2026, 10, 1, 12, 0, 0, tzinfo=timezone.utc)

    def __call__(self):
        return self.t

    def advance(self, seconds):
        self.t += timedelta(seconds=seconds)


class FakeDispatch:
    def __init__(self):
        self.jobs, self.calls, self.grants = {}, [], {}

    def create(self, request_text, repo_root, extra_allowed_tools, timeout_s, disallowed_tools=None,
               settings_path=None, pack_saved=None):
        job_id = f"job-{len(self.calls) + 1}"
        self.calls.append({"request": request_text, "extra": extra_allowed_tools, "timeout_s": timeout_s,
                           "job_id": job_id, "disallowed": disallowed_tools, "settings": settings_path,
                           "pack_saved": pack_saved})
        self.jobs[job_id] = {"status": "pending", "result": None, "request": request_text}
        return job_id

    def get(self, job_id):
        return self.jobs.get(job_id)

    def grant_once(self, request_text, tool):
        tools = self.grants.setdefault(request_text, set())
        tools.add(tool)
        return sorted(tools)


def good_form(url):
    r = schema.new_result(url)
    schema.add_page(r, 1, [{"label": "Why Acme?", "type": "textarea", "required": True}])
    return schema.finalize(r)


class FakeExtractor:
    """Stands in for jobs._run_extractor(url, out_path, timeout_s): writes a form file, or raises."""

    def __init__(self):
        self.calls, self.error, self.raw = [], None, None

    def __call__(self, url, out_path, timeout_s):
        self.calls.append({"url": url, "out": Path(out_path), "timeout_s": timeout_s})
        if self.error:
            raise self.error
        text = self.raw if self.raw is not None else json.dumps(good_form(url))
        Path(out_path).write_text(text, encoding="utf-8")


class FakeLockdown:
    """Stands in for prep_lockdown.prepare(repo_root, lid, now) -> settings path."""

    def __init__(self):
        self.calls, self.error = [], None

    def __call__(self, repo_root, lid, now):
        self.calls.append(lid)
        if self.error:
            raise self.error
        return Path(repo_root) / "lockdown" / f"{lid}.json"


def make_env(repo, clock=None, fake=None, extractor=None, lockdown=None, spawn=None):
    leads = repo / "data" / "leads"
    leads.mkdir(parents=True, exist_ok=True)
    clock = clock or Clock()
    fake = fake or FakeDispatch()
    extractor = extractor or FakeExtractor()
    lockdown = lockdown or FakeLockdown()
    svc = jobs.JobsService(repo_root=repo, leads_dir=leads, clock=clock, create_job=fake.create,
                           get_job=fake.get, grant_once=fake.grant_once, run_extractor=extractor,
                           spawn=spawn or (lambda fn: fn()), prepare_lockdown=lockdown)
    app = FastAPI()
    app.include_router(jobs.make_router(svc))
    return SimpleNamespace(svc=svc, store=svc.store, clock=clock, fake=fake, client=TestClient(app),
                           repo=repo, leads=leads, extractor=extractor, lockdown=lockdown)


@pytest.fixture
def env(tmp_path):
    return make_env(tmp_path / "repo")


def make_lead(store, n=1, **triage):
    url = f"https://jobs.example.com/acme/{n}"
    src = {"url": url, "kind": "posting", "poster_name": None, "poster_url": None, "added_via": "manual",
           "roundup_of": None, "hint": None}
    lid, _ = store.create_or_merge(url, src)
    t = {"triage_status": "complete", "company": "Acme", "role_title": "Associate", "location": "NYC",
         "pay": "$150K", "jd_url": url, "jd_read": True, "category": "vc", "relevance": "strong",
         "fit_score": "High", "fit_rationale": "fits", "bucket": "tier1",
         "bucket_reason": "strong_fit_enjoyed: fund role", "network": {"contacts": [], "summary": ""},
         "engage": None, "airtable_match": {"relation": "none"}}
    t.update(triage)
    store.update(lid, "triage", t)
    return lid


def groups_snapshot(store, lid):
    lead = store.read(lid)
    return json.dumps({"intake": lead["intake"], "triage": lead["triage"]}, sort_keys=True)


def act(env, lid, action, **kw):
    return env.client.post(f"/api/jobs/leads/{lid}/action", json={"action": action, **kw})


# ---- Task 6: list, action, undo ---------------------------------------------------

def test_lead_store_comes_from_the_skill_not_a_namespace_lib():
    import lib.lead_store as ls  # pywin32 ships a site-packages/win32/lib namespace dir
    assert Path(ls.__file__).resolve().parent.parent == jobs.SKILL_DIR.resolve()


def test_list_returns_view_with_effective_bucket(env):
    lid = make_lead(env.store, bucket="tier2")
    body = env.client.get("/api/jobs/leads").json()
    assert body["undo_window_s"] == 90 and body["server_now"] == "2026-10-01T12:00:00+00:00"
    [lead] = body["leads"]
    assert lead["lead_id"] == lid and lead["effective_bucket"] == "tier2"
    assert lead["triage"]["company"] == "Acme" and lead["decision"]["pending"] is None
    assert lead["undo_remaining_s"] is None and "history" not in lead["decision"]


def test_list_survives_corrupt_and_non_object_files(env):
    lid = make_lead(env.store)
    (env.leads / "badbadbadbad.json").write_text("{not json", encoding="utf-8")
    (env.leads / "0123456789ab.json").write_text("[1, 2]", encoding="utf-8")
    r = env.client.get("/api/jobs/leads")
    assert r.status_code == 200 and [l["lead_id"] for l in r.json()["leads"]] == [lid]


def test_action_records_pending_and_history_without_executing(env):
    lid = make_lead(env.store)
    r = act(env, lid, "skip", reason="not_interested", note="later")
    assert r.status_code == 200
    v = r.json()
    assert v["decision"]["pending"]["action"] == "skip"
    assert v["decision"]["pending"]["execute_after"] == "2026-10-01T12:01:30+00:00"
    assert v["undo_remaining_s"] == 90 and v["decision"]["decision"] is None
    dec = env.store.read(lid)["decision"]
    lead = env.store.read(lid)
    assert dec["pending"]["snapshot"] == {
        "snapshot_version": 2, "schema_version": 1,
        "bucket": "tier1", "category": "vc", "relevance": "strong", "fit_score": "High", "location": "NYC",
        "pay": "$150K", "company": "Acme", "role_title": "Associate", "warm_contact": False,
        "bucket_reason": "strong_fit_enjoyed: fund role",
        **{k: None for k in jobs.SNAPSHOT_INPUT_KEYS},
        "network": {"contacts": [], "summary": ""}, "airtable_match": {"relation": "none"},
        "first_seen": lead["intake"]["created_at"], "still_open": None,
        "config": None, "shown_score": None, "shown_rank": None, "shown_band": None}
    h = dec["history"][-1]
    assert (h["state"], h["action"], h["note"], h["at"]) == ("pending", "skip", "later",
                                                             "2026-10-01T12:00:00+00:00")
    assert h["snapshot"] == dec["pending"]["snapshot"]


@pytest.mark.parametrize("body", [
    {"action": "explode"},
    {"action": "skip"},
    {"action": "skip", "reason": "boring"},
    {"action": "skip", "reason": "location", "note": "x" * 501},
    {},
])
def test_bad_actions_are_422(env, body):
    lid = make_lead(env.store)
    assert env.client.post(f"/api/jobs/leads/{lid}/action", json=body).status_code == 422
    assert env.store.read(lid)["decision"].get("pending") is None


def test_double_tap_is_409(env):
    lid = make_lead(env.store)
    assert act(env, lid, "applied").status_code == 200
    assert act(env, lid, "skip", reason="pay").status_code == 409
    assert env.store.read(lid)["decision"]["pending"]["action"] == "applied"


@pytest.mark.parametrize("lid", ["0123456789ab", "NOTALEADID", "0123456789AB"])
def test_unknown_lead_is_404(env, lid):
    assert act(env, lid, "skip", reason="not_interested").status_code == 404
    assert env.client.post(f"/api/jobs/leads/{lid}/undo").status_code == 404


def test_undo_inside_window_cancels_and_allows_a_new_action(env):
    lid = make_lead(env.store)
    act(env, lid, "skip", reason="not_interested")
    env.clock.advance(89)
    r = env.client.post(f"/api/jobs/leads/{lid}/undo")
    assert r.status_code == 200 and r.json()["decision"]["pending"] is None
    assert env.store.read(lid)["decision"]["history"][-1]["state"] == "undone"
    assert act(env, lid, "skip", reason="pay").status_code == 200


def test_undo_at_or_after_window_is_409(env):
    lid = make_lead(env.store)
    act(env, lid, "skip", reason="not_interested")
    env.clock.advance(90)
    assert env.client.post(f"/api/jobs/leads/{lid}/undo").status_code == 409


def test_undo_with_nothing_pending_is_409(env):
    lid = make_lead(env.store)
    assert env.client.post(f"/api/jobs/leads/{lid}/undo").status_code == 409


def test_remaining_seconds_come_from_the_server_clock(env):
    lid = make_lead(env.store)
    act(env, lid, "skip", reason="not_interested")
    env.clock.advance(17)
    [lead] = env.client.get("/api/jobs/leads").json()["leads"]
    assert lead["undo_remaining_s"] == 73


def test_snapshot_flags_legacy_free_text_warm_contact(env):
    lid = make_lead(env.store, network={"contacts": [{"name": "Pat Example", "status": "Current",
                                                      "warmth": "warm (former colleague)",
                                                      "recommendation": "Referral ask"}], "summary": ""})
    act(env, lid, "skip", reason="not_interested")
    assert env.store.read(lid)["decision"]["pending"]["snapshot"]["warm_contact"] is True


@pytest.mark.parametrize("state", ["running", "starting", "extracting"])
def test_job_actions_rejected_while_a_job_runs(env, state):
    lid = make_lead(env.store)
    env.store.update(lid, "decision", {"job_status": {"kind": "prep", "state": state, "job_id": "job-x"}})
    for action in ("applied", "prep", "terminal"):
        assert act(env, lid, action).status_code == 409
    assert act(env, lid, "skip", reason="not_interested").status_code == 200


def test_applied_twice_is_409(env):
    lid = make_lead(env.store)
    env.store.update(lid, "decision", {"decision": "applied", "job_status": {"kind": "applied", "state": "done"}})
    assert act(env, lid, "applied").status_code == 409


def test_actions_and_undo_write_only_the_decision_group(env):
    lid = make_lead(env.store)
    before = groups_snapshot(env.store, lid)
    act(env, lid, "skip", reason="not_interested")
    env.client.post(f"/api/jobs/leads/{lid}/undo")
    assert groups_snapshot(env.store, lid) == before


# ---- Task 7: sweeper --------------------------------------------------------------

def test_sweep_does_nothing_inside_the_window(env):
    lid = make_lead(env.store)
    act(env, lid, "skip", reason="not_interested")
    env.clock.advance(89)
    env.svc.sweep()
    d = env.store.read(lid)["decision"]
    assert d["pending"]["action"] == "skip" and d["decision"] is None


def test_terminal_marks_needs_help_in_progress(env):
    lid = make_lead(env.store)
    act(env, lid, "terminal")
    env.clock.advance(90)
    env.svc.sweep()
    d = env.store.read(lid)["decision"]
    assert d["decision"] == "needs_help"
    assert d["job_status"] == {"kind": "terminal", "state": "in_progress", "updated_at": "2026-10-01T12:01:30+00:00"}
    assert env.fake.calls == []


def test_skip_logs_then_deletes(env):
    lid = make_lead(env.store, company="Société Générale")
    act(env, lid, "skip", reason="location", note="SF only")
    env.clock.advance(90)
    env.svc.sweep()
    assert not (env.leads / f"{lid}.json").exists()
    raw = (env.leads / "_skips.jsonl").read_text(encoding="utf-8")
    assert "Société Générale" in raw  # ensure_ascii=False
    [line] = raw.splitlines()
    rec = json.loads(line)
    assert set(rec) == {"lead_id", "canonical_url", "company", "role_title", "category", "relevance", "bucket",
                        "fit_score", "location", "pay", "warm_contact", "reason", "note", "skipped_at",
                        "snapshot_version", "schema_version", "scores", "pay_bands", "posted_date", "config",
                        "shown_score", "shown_rank"}
    assert (rec["lead_id"], rec["reason"], rec["note"], rec["bucket"]) == (lid, "location", "SF only", "tier1")
    assert rec["warm_contact"] is False and rec["skipped_at"] == "2026-10-01T12:01:30+00:00"
    assert env.client.get("/api/jobs/leads").json()["leads"] == []


def test_failed_delete_is_retried_without_a_second_log_line(env, monkeypatch):
    from lib.lead_store import LeadStoreError
    lid = make_lead(env.store)
    act(env, lid, "skip", reason="pay")
    env.clock.advance(90)
    real = env.store.delete

    def boom(x):
        raise LeadStoreError("held open")
    monkeypatch.setattr(env.store, "delete", boom)
    env.svc.sweep()
    assert (env.leads / f"{lid}.json").exists()
    monkeypatch.setattr(env.store, "delete", real)
    env.svc.sweep()
    assert not (env.leads / f"{lid}.json").exists()
    assert len((env.leads / "_skips.jsonl").read_text(encoding="utf-8").splitlines()) == 1


def test_undone_action_never_executes(env):
    lid = make_lead(env.store)
    act(env, lid, "terminal")
    env.client.post(f"/api/jobs/leads/{lid}/undo")
    env.clock.advance(300)
    env.svc.sweep()
    d = env.store.read(lid)["decision"]
    assert d["decision"] is None and d["history"][-1]["state"] == "undone"


def test_restart_resumes_pending_from_the_file(tmp_path):
    repo = tmp_path / "repo"
    first = make_env(repo)
    lid = make_lead(first.store)
    act(first, lid, "terminal")
    clock = Clock()
    clock.advance(30)
    second = make_env(repo, clock=clock)  # a new process: fresh service, same files
    second.svc.sweep()
    assert second.store.read(lid)["decision"]["pending"]["action"] == "terminal"
    clock.advance(60)
    second.svc.sweep()
    assert second.store.read(lid)["decision"]["decision"] == "needs_help"
    assert second.fake.calls == []


def test_overdue_pending_executes_on_the_startup_sweep(tmp_path):
    import time
    repo = tmp_path / "repo"
    first = make_env(repo)
    lid = make_lead(first.store)
    act(first, lid, "skip", reason="pay")
    clock = Clock()
    clock.advance(600)
    second = make_env(repo, clock=clock)
    stop = jobs.start_sweeper(second.svc, interval_s=3600)  # startup run happens immediately
    try:
        for _ in range(200):
            if not (second.leads / f"{lid}.json").exists():
                break
            time.sleep(0.01)
        assert not (second.leads / f"{lid}.json").exists()
    finally:
        stop.set()


def test_one_bad_lead_does_not_stop_the_sweep(env):
    good = make_lead(env.store, n=1)
    bad = make_lead(env.store, n=2)
    act(env, good, "terminal")
    act(env, bad, "terminal")
    p = env.store.read(bad)["decision"]["pending"]
    env.store.update(bad, "decision", {"pending": {**p, "execute_after": "garbage"}})
    env.clock.advance(90)
    env.svc.sweep()
    assert env.store.read(good)["decision"]["decision"] == "needs_help"
    assert env.store.read(bad)["decision"]["decision"] is None
    assert env.client.post(f"/api/jobs/leads/{bad}/undo").status_code == 200  # never executed, still undoable


def test_start_sweeper_repeats_until_stopped(env, monkeypatch):
    import threading
    calls = []
    two = threading.Event()

    def fake_sweep():
        calls.append(1)
        if len(calls) >= 2:
            two.set()
    monkeypatch.setattr(env.svc, "sweep", fake_sweep)
    stop = jobs.start_sweeper(env.svc, interval_s=0.01)
    assert two.wait(5)
    stop.set()


def test_sweeper_writes_only_the_decision_group(env):
    lid = make_lead(env.store)
    before = groups_snapshot(env.store, lid)
    assert act(env, lid, "terminal").status_code == 200
    env.clock.advance(90)
    env.svc.sweep()
    assert env.store.read(lid)["decision"]["decision"] == "needs_help"
    assert groups_snapshot(env.store, lid) == before


# ---- Task 8: dispatch, markers, retry ---------------------------------------------

REC = "recAbC123dEf456Gh"  # rec + 14
PREP = "data/prep/acme-2026-10-01.md"


def run_to_dispatch(env, lid, action):
    assert act(env, lid, action).status_code == 200
    env.clock.advance(90)
    env.svc.sweep()
    return env.store.read(lid)["decision"]


LEAD_IN_REQUEST = re.compile(r"leads/([0-9a-f]{12})\.json")


def form_path(lid, stamp="20261001T120130Z"):
    return f"{jobs.FORMS_DIR}{lid}-{stamp}.json"


def finish(env, job_id, **job):
    """Make the fake dispatch job reach a final state, sweep once, return that lead's decision group."""
    request = env.fake.jobs[job_id]["request"]
    lid = LEAD_IN_REQUEST.search(request).group(1)
    env.fake.jobs[job_id] = {"request": request, **job}
    env.svc.sweep()
    return env.store.read(lid)["decision"]


def test_applied_dispatches_only_after_the_window(env):
    lid = make_lead(env.store)
    act(env, lid, "applied")
    env.clock.advance(89)
    env.svc.sweep()
    assert env.fake.calls == []
    env.clock.advance(1)
    env.svc.sweep()
    [call] = env.fake.calls
    assert call["request"] == f"/job-apply --already-applied data/leads/{lid}.json"
    assert call["timeout_s"] == 1800 and call["extra"] is None and call["disallowed"] is None
    d = env.store.read(lid)["decision"]
    assert d["decision"] == "applied" and d["history"][-1]["state"] == "done"
    assert (d["job_status"]["kind"], d["job_status"]["state"], d["job_status"]["job_id"]) == ("applied", "running", "job-1")


def test_prep_extracts_then_dispatches_the_locked_down_drafter(env):
    lid = make_lead(env.store)
    d = run_to_dispatch(env, lid, "prep")
    form = form_path(lid)
    [x] = env.extractor.calls
    assert x == {"url": "https://jobs.example.com/acme/1", "out": env.repo / form, "timeout_s": 180}
    [call] = env.fake.calls
    assert call["request"] == f"/job-apply --prep data/leads/{lid}.json --form {form}"
    assert call["timeout_s"] == 1200 and call["extra"] is None
    assert call["disallowed"] == prep_lockdown.DRAFTER_DENY
    assert call["settings"] == str(env.repo / "lockdown" / f"{lid}.json") and env.lockdown.calls == [lid]
    js = d["job_status"]
    assert d["decision"] == "needs_help" and (js["kind"], js["state"], js["form_file"]) == ("prep", "running", form)


def test_running_job_is_polled_but_not_redispatched(env):
    lid = make_lead(env.store)
    run_to_dispatch(env, lid, "applied")
    env.clock.advance(30)
    env.svc.sweep()
    assert len(env.fake.calls) == 1 and env.store.read(lid)["decision"]["job_status"]["state"] == "running"


def test_a_queued_job_is_still_running_not_failed(env):
    # dispatch.py caps parallel jobs at 3 (2026-10-08); a job waiting for a slot reports "queued".
    lid = make_lead(env.store)
    run_to_dispatch(env, lid, "applied")
    env.fake.jobs["job-1"]["status"] = "queued"
    env.clock.advance(30)
    env.svc.sweep()
    assert env.store.read(lid)["decision"]["job_status"]["state"] == "running"


@pytest.mark.parametrize("text,expected", [
    (f"Logged.\nAIRTABLE_RECORD: {REC}", REC),
    (f"AIRTABLE_RECORD: {REC}\n\nSummary: done", REC),
    (f"`AIRTABLE_RECORD: {REC}`", REC),
    (f"**AIRTABLE_RECORD:** {REC}", REC),
    (f"AIRTABLE_RECORD: recAAAAAAAAAAAAAA\nAIRTABLE_RECORD: {REC}", REC),
    ("AIRTABLE_RECORD: rec123", None),
    (f"The id AIRTABLE_RECORD: {REC} is inline", None),
    ("I could not find the record.", None),
    (None, None),
])
def test_parse_airtable_record(text, expected):
    assert jobs.parse_airtable_record(text) == expected


@pytest.mark.parametrize("text,expected", [
    (f"PREP_FILE: {PREP}", PREP),
    (f"PREP_FILE: data/prep/old.md\nPREP_FILE: {PREP}", PREP),
    (f"PREP_FILE: {PREP}\n\nSummary: pack saved", PREP),
    ("PREP_FILE: `data\\prep\\acme-2026-10-01.md`", PREP),
    (f"done\n**PREP_FILE:** {PREP}\n", PREP),
    ("no marker here", None),
    (None, None),
])
def test_parse_prep_file(text, expected):
    assert jobs.parse_prep_file(text) == expected


def test_applied_completion_records_the_airtable_id(env):
    lid = make_lead(env.store)
    run_to_dispatch(env, lid, "applied")
    d = finish(env, "job-1", status="complete", result=f"Logged to Airtable.\nAIRTABLE_RECORD: {REC}")
    assert d["job_status"]["state"] == "done" and d["job_status"]["record_id"] == REC
    assert d["airtable_record_id"] == REC and d["decision"] == "applied"


def test_applied_without_marker_fails_with_the_jobs_last_line(env):
    lid = make_lead(env.store)
    run_to_dispatch(env, lid, "applied")
    d = finish(env, "job-1", status="complete", result="Searched Airtable.\nFound two matching records; stopped.")
    assert d["job_status"]["state"] == "failed" and d["decision"] == "applied"
    assert "AIRTABLE_RECORD" in d["job_status"]["error"] and "two matching records" in d["job_status"]["error"]


def test_prep_completion_records_the_prep_file(env):
    (env.repo / PREP).parent.mkdir(parents=True)
    (env.repo / PREP).write_text("# prep", encoding="utf-8")
    lid = make_lead(env.store)
    run_to_dispatch(env, lid, "prep")
    d = finish(env, "job-1", status="complete", result=f"Pack saved.\nPREP_FILE: {PREP}")
    assert d["job_status"]["state"] == "done" and d["prep_file"] == PREP and d["job_status"]["prep_file"] == PREP


@pytest.mark.parametrize("result", [
    "PREP_FILE: data/prep/missing-2026-10-01.md",
    "PREP_FILE: data/prep/acme-2026-10-01.draft.md",
    "PREP_FILE: notes/elsewhere.md",
    "PREP_FILE: data/prep/../../../../secrets.md",
    "Pack saved somewhere.",
])
def test_prep_with_a_bad_or_missing_file_fails(env, result):
    prep_dir = env.repo / "data/prep"
    prep_dir.mkdir(parents=True)
    (prep_dir / "acme-2026-10-01.draft.md").write_text("# draft", encoding="utf-8")
    (env.repo / "notes").mkdir()
    (env.repo / "notes/elsewhere.md").write_text("x", encoding="utf-8")
    lid = make_lead(env.store)
    run_to_dispatch(env, lid, "prep")
    d = finish(env, "job-1", status="complete", result=result)
    assert d["job_status"]["state"] == "failed" and not d.get("prep_file")


def test_permission_denied_then_grant_retries_with_once_tools(env):
    lid = make_lead(env.store)
    run_to_dispatch(env, lid, "applied")
    d = finish(env, "job-1", status="permission_denied", result="need gmail",
               deniedTools=["mcp__claude_ai_Gmail__search_threads"], sensitiveBlock=False, stuck=False)
    assert d["job_status"]["state"] == "needs_permission"
    assert d["job_status"]["denied_tools"] == ["mcp__claude_ai_Gmail__search_threads"]
    r = env.client.post(f"/api/jobs/leads/{lid}/retry", json={"grant": True})
    assert r.status_code == 200 and r.json()["decision"]["job_status"]["state"] == "running"
    call = env.fake.calls[-1]
    assert call["extra"] == ["mcp__claude_ai_Gmail__search_threads"] and call["timeout_s"] == 1800
    assert call["request"] == env.fake.calls[0]["request"]


@pytest.mark.parametrize("flags", [{"sensitiveBlock": True, "stuck": False}, {"sensitiveBlock": False, "stuck": True}])
def test_sensitive_or_stuck_denial_is_failed_not_grantable(env, flags):
    lid = make_lead(env.store)
    run_to_dispatch(env, lid, "applied")
    d = finish(env, "job-1", status="permission_denied", result="blocked", deniedTools=["Edit"], **flags)
    assert d["job_status"]["state"] == "failed" and "Edit" in d["job_status"]["error"]
    assert env.client.post(f"/api/jobs/leads/{lid}/retry", json={"grant": True}).status_code == 409
    assert env.client.post(f"/api/jobs/leads/{lid}/retry").status_code == 200


def test_error_fails_and_retry_redispatches(env):
    lid = make_lead(env.store)
    run_to_dispatch(env, lid, "prep")
    d = finish(env, "job-1", status="error", result="Timed out after 20 minutes")
    assert d["job_status"]["state"] == "failed" and "Timed out" in d["job_status"]["error"]
    r = env.client.post(f"/api/jobs/leads/{lid}/retry")
    assert r.status_code == 200
    assert env.fake.calls[-1]["job_id"] == "job-2" and env.fake.calls[-1]["extra"] is None
    assert env.store.read(lid)["decision"]["decision"] == "needs_help"


def test_retry_is_409_unless_failed_or_needs_permission(env):
    lid = make_lead(env.store)
    assert env.client.post(f"/api/jobs/leads/{lid}/retry").status_code == 409  # no job at all
    run_to_dispatch(env, lid, "applied")
    assert env.client.post(f"/api/jobs/leads/{lid}/retry").status_code == 409  # running
    finish(env, "job-1", status="complete", result=f"AIRTABLE_RECORD: {REC}")
    assert env.client.post(f"/api/jobs/leads/{lid}/retry").status_code == 409  # done
    other = make_lead(env.store, n=2)
    run_to_dispatch(env, other, "terminal")
    assert env.client.post(f"/api/jobs/leads/{other}/retry").status_code == 409  # terminal in_progress


def test_restart_during_a_job_marks_it_failed_then_retry(tmp_path):
    repo = tmp_path / "repo"
    first = make_env(repo)
    lid = make_lead(first.store)
    run_to_dispatch(first, lid, "applied")
    second = make_env(repo, clock=first.clock)  # new process: dispatch's in-memory registry is empty
    second.svc.sweep()
    js = second.store.read(lid)["decision"]["job_status"]
    assert js["state"] == "failed" and js["error"] == jobs.RESTART_ERROR
    r = second.client.post(f"/api/jobs/leads/{lid}/retry")
    assert r.status_code == 200 and second.fake.calls[0]["request"].startswith("/job-apply --already-applied ")


def test_dispatch_start_failure_marks_the_job_failed(env):
    def boom(*a):
        raise FileNotFoundError("claude CLI not found on PATH")
    env.svc._create_job = boom
    lid = make_lead(env.store)
    d = run_to_dispatch(env, lid, "applied")
    assert d["job_status"]["state"] == "failed" and "claude CLI not found" in d["job_status"]["error"]
    assert d["history"][-1]["state"] == "failed" and d["pending"] is None


def test_full_lifecycle_writes_only_the_decision_group(env, monkeypatch):
    lid = make_lead(env.store)
    before = groups_snapshot(env.store, lid)
    groups, real = [], env.store.update

    def spy(l, group, fields):
        groups.append(group)
        return real(l, group, fields)
    monkeypatch.setattr(env.store, "update", spy)
    run_to_dispatch(env, lid, "prep")
    finish(env, "job-1", status="error", result="boom")
    env.client.post(f"/api/jobs/leads/{lid}/retry")
    finish(env, "job-2", status="permission_denied", result="x", deniedTools=["Bash"], sensitiveBlock=False, stuck=False)
    env.client.post(f"/api/jobs/leads/{lid}/retry", json={"grant": True})
    assert set(groups) == {"decision"} and groups_snapshot(env.store, lid) == before


# ---- Task 8 fix round 1: write-first ordering, resolve() containment -------------------

def fail_update_after_dispatch(env, monkeypatch):
    """Make the first store.update after the next dispatch raise once (a failed Windows replace / lock timeout)."""
    real, state = env.store.update, {"armed": False, "fired": False}
    real_create = env.svc._create_job

    def create(*a, **kw):
        job = real_create(*a, **kw)
        state["armed"] = True
        return job

    def update(l, group, fields):
        if state["armed"] and not state["fired"]:
            state["fired"] = True
            raise OSError("replace failed")
        return real(l, group, fields)
    env.svc._create_job = create
    monkeypatch.setattr(env.store, "update", update)


def test_failed_write_after_dispatch_never_redispatches_on_sweep(env, monkeypatch):
    lid = make_lead(env.store)
    act(env, lid, "applied")
    env.clock.advance(90)
    fail_update_after_dispatch(env, monkeypatch)
    env.svc.sweep()  # dispatches, then the "running" write raises (caught by sweep)
    d = env.store.read(lid)["decision"]
    assert d["pending"] is None and d["job_status"]["state"] == "starting"
    env.svc.sweep()
    env.svc.sweep()
    assert len(env.fake.calls) == 1
    d = env.store.read(lid)["decision"]
    assert d["job_status"]["state"] == "failed" and d["job_status"]["error"] == jobs.NOT_CONFIRMED_ERROR


def test_failed_write_after_dispatch_never_redispatches_on_retry(env, monkeypatch):
    lid = make_lead(env.store)
    run_to_dispatch(env, lid, "applied")
    finish(env, "job-1", status="error", result="boom")
    fail_update_after_dispatch(env, monkeypatch)
    with pytest.raises(OSError):
        env.client.post(f"/api/jobs/leads/{lid}/retry")
    assert len(env.fake.calls) == 2
    assert env.client.post(f"/api/jobs/leads/{lid}/retry").status_code == 409  # starting is not retryable
    env.svc.sweep()
    assert env.store.read(lid)["decision"]["job_status"]["state"] == "failed"
    assert len(env.fake.calls) == 2


def test_failed_write_after_prep_dispatch_never_redispatches(env, monkeypatch):
    lid = make_lead(env.store)
    run_to_dispatch(env, lid, "prep")
    finish(env, "job-1", status="error", result="boom")
    fail_update_after_dispatch(env, monkeypatch)
    assert env.client.post(f"/api/jobs/leads/{lid}/retry").status_code == 200  # the reader thread swallowed it
    assert len(env.fake.calls) == 2
    assert env.store.read(lid)["decision"]["job_status"]["state"] == "starting"
    assert env.client.post(f"/api/jobs/leads/{lid}/retry").status_code == 409
    env.svc.sweep()
    d = env.store.read(lid)["decision"]
    assert d["job_status"]["state"] == "failed" and d["job_status"]["error"] == jobs.NOT_CONFIRMED_ERROR
    assert len(env.fake.calls) == 2


def test_starting_without_job_id_is_failed_by_sweep_without_dispatch(env):
    lid = make_lead(env.store)
    env.store.update(lid, "decision", {"decision": "applied", "job_status": {
        "kind": "applied", "state": "starting", "requested_at": "2026-10-01T12:00:00+00:00"}})
    env.svc.sweep()
    js = env.store.read(lid)["decision"]["job_status"]
    assert js["state"] == "failed" and js["error"] == jobs.NOT_CONFIRMED_ERROR and env.fake.calls == []


def test_prep_file_resolving_outside_prep_dir_is_rejected(env):
    prep_dir = env.repo / "data/prep"
    prep_dir.mkdir(parents=True)
    outside = env.repo / "outside"
    outside.mkdir()
    (outside / "pack.md").write_text("x", encoding="utf-8")
    try:
        (prep_dir / "link").symlink_to(outside, target_is_directory=True)
    except (OSError, NotImplementedError):
        pytest.skip("symlink creation not permitted on this platform")
    err = env.svc._check_prep_path(f"{PREP.rsplit('/', 1)[0]}/link/pack.md")
    assert err and "outside" in err


# ---- final review fixes ---------------------------------------------------------------

def test_timed_out_job_error_says_check_airtable(env):
    lid = make_lead(env.store)
    run_to_dispatch(env, lid, "applied")
    d = finish(env, "job-1", status="error", result="Timed out after 30 minutes")
    assert d["job_status"]["error"].endswith("— check Airtable / Prep before retrying")


@pytest.mark.parametrize("action", ["skip", "applied", "prep", "terminal"])  # move_tier*: 422, retired
def test_every_action_is_409_once_applied(env, action):
    lid = make_lead(env.store)
    env.store.update(lid, "decision", {"decision": "applied", "job_status": {"kind": "applied", "state": "done"}})
    r = act(env, lid, action, reason="pay") if action == "skip" else act(env, lid, action)
    assert r.status_code == 409


# ---- prep-extractor: extract first, then the locked-down drafter ------------------------

def test_extracting_is_written_before_the_extractor_runs(tmp_path):
    held = []
    env = make_env(tmp_path / "repo", spawn=held.append)
    lid = make_lead(env.store)
    act(env, lid, "prep")
    env.clock.advance(90)
    env.svc.sweep()
    d = env.store.read(lid)["decision"]
    assert d["pending"] is None and d["decision"] == "needs_help" and d["history"][-1]["state"] == "done"
    js = d["job_status"]
    assert (js["kind"], js["state"], js["form_file"]) == ("prep", "extracting", form_path(lid))
    assert js["request"].endswith(f"--form {form_path(lid)}")
    assert env.extractor.calls == [] and env.fake.calls == [] and len(held) == 1
    view = env.client.get("/api/jobs/leads").json()["leads"][0]
    assert view["decision"]["job_status"]["state"] == "extracting"
    assert act(env, lid, "prep").status_code == 409
    env.svc.sweep()  # the reader is alive in this process: the sweep leaves it alone
    assert env.store.read(lid)["decision"]["job_status"]["state"] == "extracting"
    held[0]()
    d = env.store.read(lid)["decision"]
    assert d["job_status"]["state"] == "running" and len(env.fake.calls) == 1


@pytest.mark.parametrize("problem", ["raises", "not_json", "wrong_shape", "too_big"])
def test_extractor_failure_still_drafts_from_a_stub(env, problem):
    if problem == "raises":
        env.extractor.error = RuntimeError("extractor timed out after 180 s")
    elif problem == "not_json":
        env.extractor.raw = "{not json"
    elif problem == "wrong_shape":
        env.extractor.raw = json.dumps({"hello": 1})
    else:
        env.extractor.raw = json.dumps({"x": "y" * (jobs.FORM_MAX_BYTES + 10)})
    lid = make_lead(env.store)
    d = run_to_dispatch(env, lid, "prep")
    form = json.loads((env.repo / form_path(lid)).read_text(encoding="utf-8"))
    assert form["schema_version"] == 1 and form["questions_visible"] is False
    assert form["errors"][0].startswith("extractor failed:")
    assert d["job_status"]["state"] == "running"
    assert env.fake.calls[0]["request"].endswith(f"--form {form_path(lid)}")


def test_a_hostile_jd_url_is_never_handed_to_the_extractor(env):
    lid = make_lead(env.store, jd_url="javascript:alert(1)")
    run_to_dispatch(env, lid, "prep")
    assert env.extractor.calls[0]["url"] == "https://jobs.example.com/acme/1"  # canonical_url fallback


def test_no_posting_url_drafts_the_standard_set(env):
    lid = make_lead(env.store, jd_url=None)
    path = env.leads / f"{lid}.json"
    data = json.loads(path.read_text(encoding="utf-8"))
    data["canonical_url"] = "not a url"
    path.write_text(json.dumps(data), encoding="utf-8")
    run_to_dispatch(env, lid, "prep")
    assert env.extractor.calls == [] and len(env.fake.calls) == 1
    form = json.loads((env.repo / form_path(lid)).read_text(encoding="utf-8"))
    assert form["errors"] == ["extractor failed: the lead has no http(s) posting URL"]


def test_lockdown_failure_fails_the_job_and_dispatches_nothing(env):
    env.lockdown.error = prep_lockdown.LockdownError("lockdown self-test failed: Write exited 0, expected 2")
    lid = make_lead(env.store)
    d = run_to_dispatch(env, lid, "prep")
    assert d["job_status"]["state"] == "failed" and env.fake.calls == []
    assert d["job_status"]["error"] == "could not start job: lockdown self-test failed: Write exited 0, expected 2"
    assert d["history"][-1]["state"] == "failed"


def test_restart_while_reading_the_form_fails_and_never_reruns(tmp_path):
    repo = tmp_path / "repo"
    first = make_env(repo, spawn=lambda fn: None)  # the reader never runs: the process "died"
    lid = make_lead(first.store)
    run_to_dispatch(first, lid, "prep")
    assert first.store.read(lid)["decision"]["job_status"]["state"] == "extracting"
    second = make_env(repo, clock=first.clock)
    second.svc.sweep()
    second.svc.sweep()
    d = second.store.read(lid)["decision"]
    assert d["job_status"]["state"] == "failed" and d["job_status"]["error"] == jobs.EXTRACT_RESTART_ERROR
    assert d["history"][-1]["state"] == "failed"
    assert second.extractor.calls == [] and second.fake.calls == []
    r = second.client.post(f"/api/jobs/leads/{lid}/retry")
    assert r.status_code == 200 and r.json()["decision"]["job_status"]["state"] == "running"
    assert len(second.extractor.calls) == 1 and len(second.fake.calls) == 1


def test_a_superseded_reader_never_launches(tmp_path):
    held = []
    env = make_env(tmp_path / "repo", spawn=held.append)
    lid = make_lead(env.store)
    run_to_dispatch(env, lid, "prep")
    env.store.update(lid, "decision", {"job_status": {"kind": "prep", "state": "failed", "error": "x"}})
    held[0]()
    assert env.fake.calls == [] and env.store.read(lid)["decision"]["job_status"]["state"] == "failed"


def test_prep_grant_is_always_409(env):
    lid = make_lead(env.store)
    run_to_dispatch(env, lid, "prep")
    js = env.store.read(lid)["decision"]["job_status"]
    env.store.update(lid, "decision", {"job_status": {**js, "state": "needs_permission", "denied_tools": ["WebFetch"]}})
    r = env.client.post(f"/api/jobs/leads/{lid}/retry", json={"grant": True})
    assert r.status_code == 409 and "locked down" in r.json()["detail"]
    assert len(env.fake.calls) == 1 and env.fake.grants == {}


def test_prep_retry_reads_the_form_again(env):
    lid = make_lead(env.store)
    run_to_dispatch(env, lid, "prep")
    finish(env, "job-1", status="error", result="boom")
    env.clock.advance(60)
    r = env.client.post(f"/api/jobs/leads/{lid}/retry")
    assert r.status_code == 200
    second = form_path(lid, "20261001T120230Z")
    assert [c["out"] for c in env.extractor.calls] == [env.repo / form_path(lid), env.repo / second]
    call = env.fake.calls[-1]
    assert call["request"].endswith(f"--form {second}")
    assert call["disallowed"] == prep_lockdown.DRAFTER_DENY and call["settings"] and call["extra"] is None


def test_prep_blocked_tool_with_a_saved_pack_is_done(env):
    (env.repo / PREP).parent.mkdir(parents=True, exist_ok=True)
    (env.repo / PREP).write_text("# prep", encoding="utf-8")
    lid = make_lead(env.store)
    run_to_dispatch(env, lid, "prep")
    d = finish(env, "job-1", status="permission_denied", result=f"Saved.\nPREP_FILE: {PREP}",
               deniedTools=["WebFetch"], sensitiveBlock=False, stuck=False)
    assert d["job_status"]["state"] == "done" and d["prep_file"] == PREP
    assert d["job_status"]["blocked_tools"] == ["WebFetch"]


def test_prep_blocked_tool_without_a_pack_fails_with_retry_only(env):
    lid = make_lead(env.store)
    run_to_dispatch(env, lid, "prep")
    d = finish(env, "job-1", status="permission_denied", result="I could not browse the page.",
               deniedTools=["WebFetch", "Bash"], sensitiveBlock=False, stuck=False)
    assert d["job_status"]["state"] == "failed"
    assert "WebFetch, Bash" in d["job_status"]["error"] and "saved no pack" in d["job_status"]["error"]
    assert env.client.post(f"/api/jobs/leads/{lid}/retry", json={"grant": True}).status_code == 409
    assert env.client.post(f"/api/jobs/leads/{lid}/retry").status_code == 200


def test_run_extractor_kills_a_hung_child(tmp_path):
    t0 = time.monotonic()
    with pytest.raises(RuntimeError, match="timed out after 2 s"):
        jobs._run_extractor("https://jobs.example.com/a", tmp_path / "f.json", 2,
                            argv_prefix=[sys.executable, "-c", "import time; time.sleep(60)"])
    assert time.monotonic() - t0 < 20


def test_run_extractor_reports_a_nonzero_exit(tmp_path):
    with pytest.raises(RuntimeError, match="exit 3: boom"):
        jobs._run_extractor("https://jobs.example.com/a", tmp_path / "f.json", 30,
                            argv_prefix=[sys.executable, "-c", "import sys; sys.stderr.write('boom\\n'); sys.exit(3)"])


def test_run_extractor_needs_its_venv(tmp_path, monkeypatch):
    monkeypatch.setattr(jobs.config, "enabled", lambda name, cfg=None: True)
    monkeypatch.setattr(jobs, "extractor_python", lambda: tmp_path / "missing" / "python.exe")
    with pytest.raises(RuntimeError, match="extractor interpreter missing"):
        jobs._run_extractor("https://jobs.example.com/a", tmp_path / "f.json", 30)


def test_run_extractor_with_the_module_off_raises_so_the_drafter_gets_a_stub(tmp_path, monkeypatch):
    monkeypatch.setattr(jobs.config, "enabled", lambda name, cfg=None: False)
    with pytest.raises(RuntimeError, match="form_extractor module is off"):
        jobs._run_extractor("https://jobs.example.com/a", tmp_path / "f.json", 30)


def test_run_extractor_passes_url_and_out_as_plain_arguments(tmp_path):
    script = ("import json, sys; a = sys.argv[1:]; "
              "open(a[a.index('--out') + 1], 'w', encoding='utf-8').write(json.dumps(a))")
    out = tmp_path / "f.json"
    url = "https://jobs.example.com/a?x=1&y=$(whoami)"
    jobs._run_extractor(url, out, 30, argv_prefix=[sys.executable, "-c", script])
    assert json.loads(out.read_text(encoding="utf-8")) == ["--url", url, "--out", str(out), "--timeout", "120"]


def test_a_valid_form_file_survives_a_nonzero_extractor_exit(env):
    good = json.dumps(good_form("https://jobs.example.com/acme/1"))

    def run(url, out_path, timeout_s):  # the CLI watchdog path: writes its file, then exits 1
        Path(out_path).write_text(good, encoding="utf-8")
        raise RuntimeError("extractor exit 1: watchdog")
    env.svc._run_extractor = run
    lid = make_lead(env.store)
    run_to_dispatch(env, lid, "prep")
    assert (env.repo / form_path(lid)).read_text(encoding="utf-8") == good
    assert len(env.fake.calls) == 1


def test_lockdown_is_prepared_outside_the_service_lock(tmp_path):
    held = []
    env = make_env(tmp_path / "repo", spawn=held.append)  # the reader runs below, not inside sweep()'s lock
    owned = []

    def probe(repo_root, lid_, now):
        owned.append(env.svc.lock._is_owned())
        return Path(repo_root) / "lockdown" / f"{lid_}.json"
    env.svc._prepare_lockdown = probe
    lid = make_lead(env.store)
    run_to_dispatch(env, lid, "prep")
    held[0]()
    assert owned == [False] and len(env.fake.calls) == 1


def test_run_extractor_bounds_the_post_kill_wait(tmp_path, monkeypatch):
    calls = []

    class HungPopen:
        pid = 1

        def __init__(self, *a, **kw):
            pass

        def communicate(self, timeout=None):
            calls.append(timeout)
            if timeout is None:
                raise AssertionError("unbounded communicate after kill")
            raise jobs.subprocess.TimeoutExpired("x", timeout)

        def kill(self):
            pass
    monkeypatch.setattr(jobs.subprocess, "Popen", HungPopen)
    monkeypatch.setattr(jobs.subprocess, "run", lambda *a, **k: None)
    if hasattr(jobs.os, "killpg"):  # the fake pid must never reach a real kill on POSIX
        monkeypatch.setattr(jobs.os, "killpg", lambda pgid, sig: None)
    with pytest.raises(RuntimeError, match="timed out after 2 s"):
        jobs._run_extractor("https://jobs.example.com/a", tmp_path / "f.json", 2, argv_prefix=["x"])
    assert calls == [2, 10]


def test_an_old_reader_never_discards_a_newer_readers_entry(tmp_path):
    held = []
    env = make_env(tmp_path / "repo", spawn=held.append)
    lid = make_lead(env.store)
    run_to_dispatch(env, lid, "prep")                      # reader A held
    env.store.update(lid, "decision", {"job_status": {"kind": "prep", "state": "failed", "error": "x"}})
    env.clock.advance(60)
    assert env.client.post(f"/api/jobs/leads/{lid}/retry").status_code == 200  # reader B held
    assert len(held) == 2
    held[0]()  # A finishes (superseded) after B started
    assert lid in env.svc._extracting_ids
    held[1]()
    assert lid not in env.svc._extracting_ids and len(env.fake.calls) == 1


def test_a_superseded_reader_does_not_prepare_a_lockdown(tmp_path):
    held = []
    env = make_env(tmp_path / "repo", spawn=held.append)
    lid = make_lead(env.store)
    run_to_dispatch(env, lid, "prep")
    env.store.update(lid, "decision", {"job_status": {"kind": "prep", "state": "failed", "error": "x"}})
    held[0]()
    assert env.lockdown.calls == []


# ---- Terminal completion inbox (2026-10-03) --------------------------------------
# /job-apply <url> --lead <lead file> appends one line to data/terminal_completions.jsonl
# after its Airtable write; the dashboard applies it on the next leads-list load.

REC2 = "rec8THGXJhfyPTyPz"


def inbox(env):
    return env.repo / "data" / "terminal_completions.jsonl"


def inbox_line(lid, rec=REC2, **over):
    d = {"lead_id": lid, "airtable_record_id": rec, "completed_at": "2026-10-01T13:00:00+00:00",
         "source": "terminal-job-apply"}
    d.update(over)
    return json.dumps(d)


def write_inbox(env, *lines):
    inbox(env).write_text("".join(l + "\n" for l in lines), encoding="utf-8")


def inbox_state(env):
    return json.loads(env.svc.inbox_state_path.read_text(encoding="utf-8"))


def terminal_lead(env, n=1):
    lid = make_lead(env.store, n=n)
    act(env, lid, "terminal")
    env.clock.advance(90)
    env.svc.sweep()
    assert env.store.read(lid)["decision"]["job_status"]["kind"] == "terminal"
    return lid


def list_view(env, lid):
    r = env.client.get("/api/jobs/leads")
    assert r.status_code == 200
    return next(l for l in r.json()["leads"] if l["lead_id"] == lid)


def test_inbox_line_moves_a_terminal_lead_to_applied(env):
    lid = terminal_lead(env)
    write_inbox(env, inbox_line(lid))
    env.clock.advance(60)
    v = list_view(env, lid)
    assert v["decision"]["decision"] == "applied" and v["decision"]["airtable_record_id"] == REC2
    d = env.store.read(lid)["decision"]
    assert d["job_status"] == {"kind": "applied", "state": "done", "record_id": REC2,
                               "updated_at": "2026-10-01T12:02:30+00:00"}
    row = d["history"][-1]
    assert (row["action"], row["via"], row["state"], row["at"]) == ("applied", "terminal", "done",
                                                                     "2026-10-01T12:02:30+00:00")
    assert row["record_id"] == REC2 and row["completed_at"] == "2026-10-01T13:00:00+00:00"
    assert env.fake.calls == []  # nothing dispatched
    assert inbox(env).read_text(encoding="utf-8") == inbox_line(lid) + "\n"  # never rewritten
    [entry] = inbox_state(env)["lines"].values()
    assert entry["outcome"] == "applied" and entry["lead_id"] == lid


def test_inbox_is_idempotent_across_reloads_and_duplicate_lines(env):
    lid = terminal_lead(env)
    write_inbox(env, inbox_line(lid), inbox_line(lid))
    env.client.get("/api/jobs/leads")
    first = env.store.read(lid)["decision"]
    env.clock.advance(60)
    env.client.get("/api/jobs/leads")
    env.svc.inbox_state_path.unlink()  # even with the sidecar lost, a re-read is a no-op
    env.client.get("/api/jobs/leads")
    assert env.store.read(lid)["decision"] == first
    assert sum(1 for h in first["history"] if h.get("via") == "terminal") == 1


@pytest.mark.parametrize("setup", ["untouched", "pending_terminal", "prep_done"])
def test_inbox_ignores_a_lead_not_in_terminal_progress(env, setup):
    lid = make_lead(env.store)
    if setup == "pending_terminal":
        act(env, lid, "terminal")  # still inside its undo window
    if setup == "prep_done":
        env.store.update(lid, "decision", {"decision": "needs_help",
                                           "job_status": {"kind": "prep", "state": "done"}})
    before = env.store.read(lid)["decision"]
    write_inbox(env, inbox_line(lid))
    env.client.get("/api/jobs/leads")
    assert env.store.read(lid)["decision"] == before
    assert inbox(env).read_text(encoding="utf-8").strip() == inbox_line(lid)  # kept
    [entry] = inbox_state(env)["lines"].values()
    assert entry["outcome"] == "deferred" and entry["reason"]


def test_a_deferred_line_applies_once_the_lead_reaches_terminal(env):
    lid = make_lead(env.store)
    act(env, lid, "terminal")
    write_inbox(env, inbox_line(lid))
    env.client.get("/api/jobs/leads")  # pending: deferred, not applied
    assert env.store.read(lid)["decision"]["decision"] is None
    env.clock.advance(90)
    env.svc.sweep()
    env.client.get("/api/jobs/leads")
    assert env.store.read(lid)["decision"]["decision"] == "applied"


@pytest.mark.parametrize("rec", ["rec123", "recAbC123dEf456Gh7", "xyzAbC123dEf456Gh", "rec8THGXJhfyPT-z", None, 5])
def test_inbox_rejects_a_bad_record_id(env, rec):
    lid = terminal_lead(env)
    write_inbox(env, json.dumps({"lead_id": lid, "airtable_record_id": rec, "completed_at": "x",
                                 "source": "terminal-job-apply"}))
    env.client.get("/api/jobs/leads")
    d = env.store.read(lid)["decision"]
    assert d["decision"] == "needs_help" and d["job_status"]["kind"] == "terminal"
    [entry] = inbox_state(env)["lines"].values()
    assert entry["outcome"] == "rejected" and "airtable_record_id" in entry["reason"]


@pytest.mark.parametrize("lid", ["0123456789ab", "../../x", "ABCDEF012345"])
def test_inbox_rejects_an_unknown_or_malformed_lead(env, lid):
    write_inbox(env, inbox_line(lid))
    r = env.client.get("/api/jobs/leads")
    assert r.status_code == 200
    [entry] = inbox_state(env)["lines"].values()
    assert entry["outcome"] == "rejected"


def test_corrupt_lines_never_break_the_list_and_good_lines_still_apply(env):
    lid = terminal_lead(env)
    write_inbox(env, "{not json", "[1, 2]", json.dumps({"lead_id": lid}), inbox_line(lid, source="other"),
                inbox_line(lid))
    r = env.client.get("/api/jobs/leads")
    assert r.status_code == 200
    assert env.store.read(lid)["decision"]["decision"] == "applied"
    outcomes = sorted(e["outcome"] for e in inbox_state(env)["lines"].values())
    assert outcomes == ["applied", "rejected", "rejected", "rejected", "rejected"]
    assert len(inbox(env).read_text(encoding="utf-8").splitlines()) == 5  # nothing deleted


def test_a_partial_last_line_is_left_for_the_next_load(env):
    lid = terminal_lead(env)
    inbox(env).write_text(inbox_line(lid)[:30], encoding="utf-8")  # writer mid-append: no newline yet
    env.client.get("/api/jobs/leads")
    assert not env.svc.inbox_state_path.exists() or inbox_state(env)["lines"] == {}
    inbox(env).write_text(inbox_line(lid) + "\n", encoding="utf-8")
    env.client.get("/api/jobs/leads")
    assert env.store.read(lid)["decision"]["decision"] == "applied"


@pytest.mark.parametrize("state", ["running", "starting", "extracting"])
def test_inbox_never_touches_a_lead_with_an_active_job(env, state):
    lid = make_lead(env.store)
    env.store.update(lid, "decision", {"decision": "needs_help",
                                       "job_status": {"kind": "terminal", "state": state}})
    before = env.store.read(lid)["decision"]
    write_inbox(env, inbox_line(lid))
    env.client.get("/api/jobs/leads")
    assert env.store.read(lid)["decision"] == before


def test_inbox_on_an_already_applied_lead_is_a_noop(env):
    lid = make_lead(env.store)
    run_to_dispatch(env, lid, "applied")
    finish(env, "job-1", status="complete", result=f"AIRTABLE_RECORD: {REC}")
    before = env.store.read(lid)["decision"]
    write_inbox(env, inbox_line(lid))
    env.client.get("/api/jobs/leads")
    assert env.store.read(lid)["decision"] == before
    assert [e["outcome"] for e in inbox_state(env)["lines"].values()] == ["noop"]


def test_missing_inbox_or_corrupt_sidecar_is_harmless(env):
    lid = terminal_lead(env)
    assert env.client.get("/api/jobs/leads").status_code == 200  # no inbox file at all
    env.svc.inbox_state_path.parent.mkdir(parents=True, exist_ok=True)
    env.svc.inbox_state_path.write_text("{broken", encoding="utf-8")
    write_inbox(env, inbox_line(lid))
    assert env.client.get("/api/jobs/leads").status_code == 200
    assert env.store.read(lid)["decision"]["decision"] == "applied"


def test_inbox_processing_errors_never_break_the_list(env, monkeypatch):
    make_lead(env.store)
    monkeypatch.setattr(env.svc, "_apply_inbox_line", lambda *a, **k: 1 / 0)
    write_inbox(env, inbox_line("0123456789ab"))
    r = env.client.get("/api/jobs/leads")
    assert r.status_code == 200 and len(r.json()["leads"]) == 1


def test_inbox_writes_only_the_decision_group_and_never_inside_leads(env):
    lid = terminal_lead(env)
    before = groups_snapshot(env.store, lid)
    write_inbox(env, inbox_line(lid))
    env.client.get("/api/jobs/leads")
    assert groups_snapshot(env.store, lid) == before
    assert env.svc.inbox_state_path.parent == inbox(env).parent
    assert sorted(p.name for p in env.leads.iterdir()) == [f"{lid}.json"]


# ---- Fallback: "Applied (done in terminal)" ----------------------------------------

def test_applied_from_a_terminal_card_dispatches_from_terminal(env):
    lid = terminal_lead(env)
    d = run_to_dispatch(env, lid, "applied")
    [call] = env.fake.calls
    assert call["request"] == (f"/job-apply --already-applied data/leads/{lid}.json"
                               " --from-terminal")
    assert d["job_status"]["from_terminal"] is True


def test_from_terminal_survives_a_retry(env):
    lid = terminal_lead(env)
    run_to_dispatch(env, lid, "applied")
    finish(env, "job-1", status="error", result="boom")
    assert env.client.post(f"/api/jobs/leads/{lid}/retry").status_code == 200
    assert env.fake.calls[-1]["request"].endswith(" --from-terminal")


def test_applied_from_a_tier_card_has_no_from_terminal(env):
    lid = make_lead(env.store)
    d = run_to_dispatch(env, lid, "applied")
    assert not env.fake.calls[0]["request"].endswith("--from-terminal")
    assert "from_terminal" not in d["job_status"]


# ---- ranking engine Task 11a: v3 view keys, snapshot v2, shown, tier moves retired ---------

V3 = {"route": "board", "job_summary": "Run grid analytics product", "main_gap": "No PM title", "level": "senior",
      "scores": {"role_fit": {"score": 70, "evidence": "fits"}, "domain": {"score": 90, "evidence": "climate software"}},
      "locations": [{"label": "New York, NY", "metro": "new_york"}], "modality": "hybrid",
      "pay_bands": [{"location_metro": "new_york", "min": 140000, "max": 160000, "period": "year", "basis": "base"}],
      "pay_source": "posting", "posted_date": "2026-10-10", "applicant_count": 40, "inbound": None,
      "category": None, "relevance": None, "bucket": None, "bucket_reason": None}


def make_v2_lead(store, n=1, **triage):
    lid = make_lead(store, n, **{**V3, **triage})
    store.update(lid, "triage", {}, schema_version=2)
    return lid


@pytest.mark.parametrize("action", ["move_tier1", "move_tier2"])
def test_tier_moves_are_retired(env, action):
    lid = make_v2_lead(env.store)
    assert act(env, lid, action).status_code == 422 and "move_tier" not in ", ".join(jobs.ACTIONS)


def test_view_exposes_v3_keys_schema_version_and_first_seen(env):
    lid = make_v2_lead(env.store)
    [v] = env.client.get("/api/jobs/leads").json()["leads"]
    assert v["schema_version"] == 2 and v["first_seen"] == env.store.read(lid)["intake"]["created_at"]
    assert v["effective_bucket"] is None                                  # v2 leads carry no tier
    for k in ("route", "scores", "job_summary", "main_gap", "pay_bands", "pay_source", "locations", "modality",
              "posted_date", "applicant_count", "inbound", "level", "triage_status"):
        assert k in v["triage"], k


def test_snapshot_v2_carries_inputs_config_and_what_the_user_saw(tmp_path):
    import jobs_scoring_config as jsc
    env = make_env(tmp_path / "repo")
    env.svc.config_store = jsc.ConfigStore(tmp_path / "cfg.json")
    lid = make_v2_lead(env.store, network={"contacts": [{"name": "Pat", "status": "current", "warmth": "warm"}]})
    act(env, lid, "skip", reason="pay", shown={"score": 81, "rank": 2, "band": "apply_now"})
    snap = env.store.read(lid)["decision"]["pending"]["snapshot"]
    assert snap["snapshot_version"] == 2 and snap["schema_version"] == 2 and snap["bucket"] is None
    assert snap["scores"]["domain"]["score"] == 90
    assert snap["config"] == {"version": 0, "weights": jsc.defaults()["active_weights"], "floor": 100000,
                              "bands": {"apply_now_min": 75, "worth_a_look_min": 55}}
    assert (snap["shown_score"], snap["shown_rank"], snap["shown_band"], snap["warm_contact"]) == (81, 2, "apply_now", True)
    assert snap["pay_bands"][0]["min"] == 140000 and snap["posted_date"] == "2026-10-10"


def test_snapshot_carries_every_input_scoring_bridge_needs_to_rescore(env):
    lid = make_v2_lead(env.store, relocation_assistance=True, posting_status="open", triaged_at="2026-10-12T09:00:00+00:00",
                       recruiter_signal="recruiter reached out", pay_type="salary")
    # decision.still_open joins the skill's GROUPS in Task 11b/11c; write the file directly until then
    path = env.leads / f"{lid}.json"
    raw = json.loads(path.read_text(encoding="utf-8"))
    raw["decision"]["still_open"] = {"checked_at": "2026-10-12", "open": True}
    path.write_text(json.dumps(raw), encoding="utf-8")
    act(env, lid, "skip", reason="pay")
    lead = env.store.read(lid)
    snap = lead["decision"]["pending"]["snapshot"]
    for k in ("scores", "level", "pay_type", "pay_bands", "pay_source", "bonus", "equity", "locations", "modality",
              "posted_date", "reposted", "applicant_count", "inbound", "route", "network", "airtable_match",
              "relocation_assistance", "posting_status", "recruiter_signal", "triaged_at", "first_seen", "still_open"):
        assert k in snap, k
    assert (snap["relocation_assistance"], snap["posting_status"], snap["recruiter_signal"], snap["pay_type"]) == (
        True, "open", "recruiter reached out", "salary")
    assert snap["triaged_at"] == "2026-10-12T09:00:00+00:00" and snap["airtable_match"] == {"relation": "none"}
    assert snap["first_seen"] == lead["intake"]["created_at"]
    assert snap["still_open"] == {"checked_at": "2026-10-12", "open": True}


def test_view_exposes_route_and_schema_version_for_a_v1_screened_lead(env):
    lid = make_lead(env.store, route="screened", bucket=None)         # skill-side: screened leads may stay v1
    [v] = env.client.get("/api/jobs/leads").json()["leads"]
    assert (v["lead_id"], v["schema_version"], v["triage"]["route"]) == (lid, 1, "screened")


def test_shown_is_optional_for_ordinary_actions(env):
    lid = make_v2_lead(env.store)
    assert act(env, lid, "skip", reason="pay").status_code == 200
    assert env.store.read(lid)["decision"]["pending"]["snapshot"]["shown_score"] is None


@pytest.mark.parametrize("shown", [{"score": 0, "rank": 1, "band": "low"}, {"score": 101, "rank": 1, "band": "low"},
                                   {"score": 50, "rank": 0, "band": "low"}, {"score": "high", "rank": 1, "band": "low"},
                                   {"score": 50, "rank": 1, "band": "tier1"}, {"score": 50, "rank": 1},
                                   {"score": 50, "rank": 1, "band": "low", "x": 1}])
def test_bad_shown_is_422(env, shown):
    lid = make_v2_lead(env.store)
    assert act(env, lid, "skip", reason="pay", shown=shown).status_code == 422


def test_skip_record_v2_keeps_v1_keys_and_adds_scores(env):
    lid = make_v2_lead(env.store)
    act(env, lid, "skip", reason="closed_stale", note="closed-check 2026-10-14",
        shown={"score": 66, "rank": 9, "band": "worth_a_look"})
    env.clock.advance(90)
    env.svc.sweep()
    line = json.loads((env.leads / "_skips.jsonl").read_text(encoding="utf-8").splitlines()[-1])
    assert line["reason"] == "closed_stale" and line["category"] is None and line["shown_score"] == 66
    assert line["scores"]["role_fit"]["score"] == 70 and line["snapshot_version"] == 2 and "bucket" in line


def test_dispatch_off_switch_refuses_job_actions_with_503(tmp_path):
    env = make_env(tmp_path / "repo")
    env.svc.dispatch_enabled = False
    lid = make_v2_lead(env.store)
    for a in ("applied", "prep", "terminal"):
        r = act(env, lid, a)
        assert r.status_code == 503 and "job_apply module is off" in r.json()["detail"], a
    assert env.client.post(f"/api/jobs/leads/{lid}/retry").status_code == 503
    assert act(env, lid, "skip", reason="pay").status_code == 200       # non-dispatch actions still work
    assert env.fake.calls == []


# ---- Task 11b: band moves ------------------------------------------------------
SHOWN = {"score": 61, "rank": 7, "band": "worth_a_look"}
OV = lambda band, computed: {"band_override": {"band": band, "computed_band": computed, "score": 30,
                                               "set_at": "2026-10-01T00:00:00+00:00"}}


def move(env, lid, action, shown=SHOWN):
    return act(env, lid, action, **({"shown": shown} if shown is not None else {}))


def settle(env):
    env.clock.advance(90)
    env.svc.sweep()


def test_move_up_is_pending_then_stores_the_override_with_a_snapshot(env):
    lid = make_v2_lead(env.store)
    r = move(env, lid, "move_up")
    assert r.status_code == 200 and r.json()["decision"]["pending"]["target_band"] == "apply_now"
    settle(env)
    d = env.store.read(lid)["decision"]
    assert d["band_override"] == {"band": "apply_now", "computed_band": "worth_a_look", "score": 61,
                                  "set_at": env.clock().isoformat()} and d["pending"] is None
    h = d["history"][-1]
    assert (h["action"], h["state"]) == ("move_up", "done")
    assert (h["snapshot"]["computed_band"], h["snapshot"]["chosen_band"], h["snapshot"]["shown_score"],
            h["snapshot"]["snapshot_version"]) == ("worth_a_look", "apply_now", 61, 2)
    [v] = env.client.get("/api/jobs/leads").json()["leads"]
    assert v["decision"]["band_override"]["band"] == "apply_now"


@pytest.mark.parametrize("action,band", [("move_up", "apply_now"), ("move_down", "low")])
def test_moves_past_the_edges_are_409(env, action, band):
    lid = make_v2_lead(env.store)
    assert move(env, lid, action, {"score": 50, "rank": 1, "band": band}).status_code == 409


def test_moves_step_from_the_stored_override_not_the_score(env):
    lid = make_v2_lead(env.store)
    env.store.update(lid, "decision", OV("apply_now", "low"))
    assert move(env, lid, "move_up", {"score": 30, "rank": 9, "band": "low"}).status_code == 409   # already top
    move(env, lid, "move_down", {"score": 30, "rank": 9, "band": "low"})
    settle(env)
    assert env.store.read(lid)["decision"]["band_override"]["band"] == "worth_a_look"


def test_a_move_back_onto_the_computed_band_clears_the_override(env):
    lid = make_v2_lead(env.store)
    env.store.update(lid, "decision", OV("apply_now", "worth_a_look"))
    move(env, lid, "move_down")
    settle(env)
    assert env.store.read(lid)["decision"]["band_override"] is None


def test_reset_clears_the_override_needs_one_and_needs_shown(env):
    lid = make_v2_lead(env.store)
    assert move(env, lid, "reset_band").status_code == 409
    env.store.update(lid, "decision", OV("low", "apply_now"))
    assert move(env, lid, "reset_band", shown=None).status_code == 422
    assert move(env, lid, "reset_band").status_code == 200
    settle(env)
    d = env.store.read(lid)["decision"]
    assert d["band_override"] is None and d["history"][-1]["snapshot"]["chosen_band"] is None


def test_undo_inside_the_window_leaves_no_override(env):
    lid = make_v2_lead(env.store)
    move(env, lid, "move_up")
    assert env.client.post(f"/api/jobs/leads/{lid}/undo").status_code == 200
    settle(env)
    assert not env.store.read(lid)["decision"].get("band_override")


def test_a_gated_lead_can_be_moved_up(env):
    lid = make_v2_lead(env.store, pay_type="unpaid", pay_bands=[])
    assert move(env, lid, "move_up", {"score": 40, "rank": 30, "band": "low"}).status_code == 200
    settle(env)
    assert env.store.read(lid)["decision"]["band_override"]["band"] == "worth_a_look"


@pytest.mark.parametrize("shown", [None, {"score": 61, "rank": 7, "band": "tier1"}])
def test_moves_need_a_valid_shown_band(env, shown):
    lid = make_v2_lead(env.store)
    assert move(env, lid, "move_up", shown).status_code == 422


@pytest.mark.parametrize("setup", ["v1", "closed", "screened", "needs_help"])
def test_only_board_cards_move(env, setup):
    lid = make_v2_lead(env.store) if setup != "v1" else make_lead(env.store)
    if setup in ("closed", "screened"):
        env.store.update(lid, "triage", {"route": setup})
    if setup == "needs_help":
        env.store.update(lid, "decision", {"decision": "needs_help"})
    assert move(env, lid, "move_up").status_code == 409


def test_the_override_survives_a_re_triage(env):
    lid = make_v2_lead(env.store)
    move(env, lid, "move_up")
    settle(env)
    env.store.update(lid, "triage", {"scores": {"role_fit": {"score": 20, "evidence": "new"},
                                                "domain": {"score": 20, "evidence": "new"}}}, schema_version=2)
    assert env.store.read(lid)["decision"]["band_override"]["band"] == "apply_now"


def test_step_band_mirrors_the_js_stepper():
    assert jobs.step_band("low", "move_up") == "worth_a_look"
    assert jobs.step_band("apply_now", "move_up") is None
    assert jobs.step_band("low", "move_down") is None
    assert jobs.step_band("bogus", "move_up") is None


# ---- Task 11c: Still open -------------------------------------------------------------

def test_still_open_returns_a_closed_card_to_the_board_and_is_logged(env):
    lid = make_v2_lead(env.store, route="closed", posting_status="closed")
    assert act(env, lid, "still_open", shown=None).status_code == 200
    settle(env)
    d = env.store.read(lid)["decision"]
    assert d["still_open"]["at"] == env.clock().isoformat() and d["history"][-1]["action"] == "still_open"
    assert env.store.read(lid)["triage"]["route"] == "closed"            # triage untouched (single writer)
    [v] = env.client.get("/api/jobs/leads").json()["leads"]
    assert v["decision"]["still_open"]["at"]
    assert move(env, lid, "move_up").status_code == 200                  # now a board card: it can move


def test_still_open_is_only_for_closed_cards(env):
    lid = make_v2_lead(env.store)
    assert act(env, lid, "still_open").status_code == 409


def test_still_open_expires_when_a_newer_triage_says_closed(env):
    lid = make_v2_lead(env.store, route="closed", posting_status="closed", triaged_at="2026-10-12T08:00:00+00:00")
    act(env, lid, "still_open")
    settle(env)
    lead = env.store.read(lid)
    assert lead["decision"]["still_open"]["triaged_at_seen"] == "2026-10-12T08:00:00+00:00"
    assert jobs.still_open_active(lead)
    assert act(env, lid, "still_open").status_code == 409                # already active
    env.store.update(lid, "triage", {"triaged_at": "2026-10-20T08:00:00+00:00", "route": "closed"}, schema_version=2)
    assert not jobs.still_open_active(env.store.read(lid))
    assert move(env, lid, "move_up").status_code == 409                  # back on Closed?: not a board card
    assert act(env, lid, "still_open").status_code == 200                # the user can say so again
    settle(env)
    assert env.store.read(lid)["decision"]["still_open"]["triaged_at_seen"] == "2026-10-20T08:00:00+00:00"


def test_still_open_records_the_triage_the_user_saw_at_tap_time(env):
    lid = make_v2_lead(env.store, route="closed", posting_status="closed", triaged_at="2026-10-12T08:00:00+00:00")
    assert act(env, lid, "still_open").status_code == 200
    env.store.update(lid, "triage", {"triaged_at": "2026-10-13T09:00:00+00:00"}, schema_version=2)  # re-triage in the window
    settle(env)
    lead = env.store.read(lid)
    assert lead["decision"]["still_open"]["triaged_at_seen"] == "2026-10-12T08:00:00+00:00"
    assert not jobs.still_open_active(lead)


def test_still_open_active_matches_the_js_rule():
    so = {"at": "x", "by": "user", "triaged_at_seen": "A"}
    assert not jobs.still_open_active({})
    assert not jobs.still_open_active({"decision": {"still_open": None}, "triage": {}})
    assert jobs.still_open_active({"decision": {"still_open": so}, "triage": {}})                    # no triaged_at
    assert jobs.still_open_active({"decision": {"still_open": so}, "triage": {"triaged_at": "A"}})
    assert not jobs.still_open_active({"decision": {"still_open": so}, "triage": {"triaged_at": "B"}})


def test_retired_tier_code_is_gone():
    src = (BACKEND / "jobs.py").read_text(encoding="utf-8")
    assert "_exec_move_tier" not in src and "lib.buckets" not in src


def test_view_exposes_the_newest_source_note(env):
    lid = make_v2_lead(env.store)
    env.store.append_source(lid, {"url": "https://slack.example/1", "kind": "posting", "poster_name": None,
                                  "poster_url": None, "added_via": "slack", "roundup_of": None, "hint": None,
                                  "note": "Dana's team, he'll refer me"})
    [v] = env.client.get("/api/jobs/leads").json()["leads"]
    assert v["source_note"] == "Dana's team, he'll refer me"


def test_view_source_note_is_none_without_notes(env):
    make_v2_lead(env.store)
    [v] = env.client.get("/api/jobs/leads").json()["leads"]
    assert v["source_note"] is None


# --- Follow-ups 2026-10-15: the dispatch job list applies the same "pack saved means done" rule ---

def test_prep_dispatch_carries_a_pack_saved_check_that_matches_the_card_rule(env):
    lid = make_lead(env.store)
    run_to_dispatch(env, lid, "prep")
    check = env.fake.calls[-1]["pack_saved"]
    assert callable(check)
    assert check(f"Saved.\nPREP_FILE: {PREP}") is False            # marker, but the file is not on disk
    (env.repo / PREP).parent.mkdir(parents=True, exist_ok=True)
    (env.repo / PREP).write_text("# prep", encoding="utf-8")
    assert check(f"Saved.\nPREP_FILE: {PREP}") is True
    assert check("no marker at all") is False
    assert check(f"PREP_FILE: {PREP[:-3]}.draft.md") is False        # the frozen twin is never the pack
    assert check("PREP_FILE: notes/profile.md") is False


def test_applied_dispatch_gets_no_pack_saved_check(env):
    lid = make_lead(env.store)
    run_to_dispatch(env, lid, "applied")
    assert env.fake.calls[-1]["pack_saved"] is None


def test_prep_complete_with_blocked_tools_keeps_them_on_the_card(env):
    (env.repo / PREP).parent.mkdir(parents=True, exist_ok=True)
    (env.repo / PREP).write_text("# prep", encoding="utf-8")
    lid = make_lead(env.store)
    run_to_dispatch(env, lid, "prep")
    d = finish(env, "job-1", status="complete", result=f"Saved.\nPREP_FILE: {PREP}", blockedTools=["Glob", "Grep"])
    assert d["job_status"]["state"] == "done" and d["prep_file"] == PREP
    assert d["job_status"]["blocked_tools"] == ["Glob", "Grep"]
