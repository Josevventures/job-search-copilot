"""Server wiring for the standalone board: routers mounted, sweeper started by the app lifespan (not at import),
page and scripts served, prep packs served only from the prep folder.

Run from the repo root:  python -m pytest board/tests -q
"""
import sys
import threading
from pathlib import Path

import pytest

BACKEND = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BACKEND))


@pytest.fixture()
def server_mod(monkeypatch, tmp_path):
    import server
    from lib.lead_store import LeadStore
    monkeypatch.setattr(server.JOBS, "store", LeadStore(tmp_path / "leads"))  # never a real data dir
    monkeypatch.setattr(server.JOBS_CONFIG, "path", tmp_path / "cfg.json")
    return server


def test_jobs_api_is_mounted(server_mod):
    from fastapi.testclient import TestClient
    r = TestClient(server_mod.app).get("/api/jobs/leads")
    assert r.status_code == 200 and r.json()["leads"] == []
    assert r.json()["leads_dir"].endswith("leads/") and isinstance(r.json()["dispatch_enabled"], bool)


def test_lifespan_starts_and_stops_the_sweeper(server_mod, monkeypatch):
    from fastapi.testclient import TestClient
    started = {}
    stop = threading.Event()

    def fake_start(svc, interval_s=5):
        started["svc"] = svc
        return stop
    monkeypatch.setattr(server_mod.jobs_mod, "start_sweeper", fake_start)
    with TestClient(server_mod.app):
        assert started["svc"] is server_mod.JOBS and not stop.is_set()
    assert stop.is_set()


def test_jobs_routes_are_registered(server_mod):
    from fastapi.testclient import TestClient
    paths = {getattr(r, "path", None) for r in server_mod.app.routes}
    assert {"/jobs", "/jobs.js", "/jobs_scoring.js", "/"} <= paths
    r = TestClient(server_mod.app).post("/api/jobs/leads/0123456789ab/retry")
    assert r.status_code in (404, 503)   # 503 when the job_apply module is off, else "no lead file"


def test_root_redirects_to_the_board(server_mod):
    from fastapi.testclient import TestClient
    r = TestClient(server_mod.app).get("/", follow_redirects=False)
    assert r.status_code in (302, 307) and r.headers["location"] == "/jobs"


def test_jobs_page_and_script_are_served(server_mod):
    from fastapi.testclient import TestClient
    c = TestClient(server_mod.app)
    page = c.get("/jobs")
    assert page.status_code == 200 and page.headers["content-type"].startswith("text/html")
    assert '<script src="/jobs.js"></script>' in page.text and "JobsLogic" in page.text
    assert 'aria-current="page">Jobs board<' in page.text
    assert "innerHTML" not in page.text  # lead text only ever goes in via textContent
    js = c.get("/jobs.js")
    assert js.status_code == 200 and "javascript" in js.headers["content-type"]
    assert "module.exports" in js.text and "JobsLogic" in js.text


def test_scoring_config_routes_are_mounted_and_use_jobs_config(server_mod, tmp_path):
    from fastapi.testclient import TestClient
    c = TestClient(server_mod.app)
    assert c.get("/api/jobs/scoring-config").json()["config"]["version"] == 0
    r = c.post("/api/jobs/scoring-config", json={"expected_version": 0, "active_preset": "Balanced",
               "active_weights": {"role_fit": 1, "domain": 1, "odds": 1, "comp": 1, "location": 1, "freshness": 1},
               "compensation_settings": {"annual_base_floor": 95000}})
    assert r.status_code == 200 and (tmp_path / "cfg.json").exists()


def test_build_takes_the_config_paths_and_the_dispatch_switch(server_mod, tmp_path):
    svc, store = server_mod.build(leads_dir=tmp_path / "L", config_path=tmp_path / "c.json", dispatch_enabled=False)
    assert svc.store.base == tmp_path / "L" and store.path == tmp_path / "c.json" and svc.dispatch_enabled is False


def test_prep_files_are_served_only_from_the_prep_folder(server_mod, monkeypatch, tmp_path):
    from fastapi.testclient import TestClient
    prep = tmp_path / "data" / "prep"
    prep.mkdir(parents=True)
    (prep / "acme.md").write_text("# prep", encoding="utf-8")
    (tmp_path / "data" / "secret.md").write_text("no", encoding="utf-8")
    monkeypatch.setattr(server_mod.config, "prep_dir", lambda cfg=None: prep)
    monkeypatch.setattr(server_mod.jobs_mod, "REPO_ROOT", tmp_path)
    c = TestClient(server_mod.app)
    assert c.get("/api/files/data/prep/acme.md").text == "# prep"
    for bad in ("data/secret.md", "data/prep/../secret.md", "data/prep/missing.md"):
        assert c.get(f"/api/files/{bad}").status_code == 404, bad


def test_scorer_script_is_served(server_mod):
    from fastapi.testclient import TestClient
    r = TestClient(server_mod.app).get("/jobs_scoring.js")
    assert r.status_code == 200 and "JobsScoring" in r.text and "javascript" in r.headers["content-type"]


def _inline_script(html):
    import re
    return max(re.findall(r"<script>(.*?)</script>", html, re.S), key=len)


def test_jobs_page_loads_the_scorer_first_and_has_no_tier_moves(server_mod):
    from fastapi.testclient import TestClient
    html = TestClient(server_mod.app).get("/jobs").text
    assert html.index('src="/jobs_scoring.js"') < html.index('src="/jobs.js"')
    assert "move_tier" not in html and "bucketLabel" not in html
    s = _inline_script(html)
    for name in ("function liveConfig", "function rerank", "function patchScore", "function settingsDirty",
                 "band-head", "L.bandHeaders", "L.shownFor", "R.DEFAULT_CONFIG"):
        assert name in s, name


def test_jobs_page_inline_script_parses(server_mod, tmp_path):
    import shutil
    import subprocess
    from fastapi.testclient import TestClient
    if shutil.which("node") is None:
        pytest.skip("node not on PATH")
    f = tmp_path / "inline.js"
    f.write_text(_inline_script(TestClient(server_mod.app).get("/jobs").text), encoding="utf-8")
    assert subprocess.run(["node", "--check", str(f)], capture_output=True).returncode == 0


def test_jobs_page_has_the_ranking_drawer(server_mod):
    from fastapi.testclient import TestClient
    html = TestClient(server_mod.app).get("/jobs").text
    for i in ("rank-btn", "rank-sheet", "rank-presets", "rank-sliders", "rank-floor", "rank-floor-minus",
              "rank-floor-plus", "rank-apply", "rank-apply-minus", "rank-apply-plus", "rank-worth",
              "rank-worth-minus", "rank-worth-plus", "rank-bands-error", "rank-conflict", "rank-reload",
              "rank-save", "rank-revert", "rank-close"):
        assert f'id="{i}"' in html, i
    assert 'inputmode="numeric"' in html
    s = _inline_script(html)
    assert "L.rankButtonText" in s and "L.settingsSaveOutcome" in s
    assert "Set at least one weight above 0" in s
    assert "await loadConfig(); }" not in s         # a 409 never auto-reloads over unsaved edits
