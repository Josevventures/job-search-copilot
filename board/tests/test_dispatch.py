"""Tests for dispatch.py's headless-run contract: the preamble, the raw request kept for the log and the
once-grant key, timeouts, flag order, and the locked-down prep drafter's "a saved pack is a success" rule.

Run from the repo root:  python -m pytest board/tests -q
"""
import json
import sys
from pathlib import Path

BACKEND = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BACKEND))

import dispatch  # noqa: E402


def test_prompt_ends_with_request_verbatim():
    req = "/job-apply --prep data/leads/0123456789ab.json --form data/prep/_forms/f.json"
    prompt = dispatch.build_dispatch_prompt(req)
    assert prompt.endswith(req)
    assert prompt.startswith("[Dispatch mode]")


def test_prompt_forbids_the_claude_dir_and_git():
    prompt = dispatch.build_dispatch_prompt("x")
    assert ".claude" in prompt          # names the blocked path so the model recognizes it
    assert "Do not commit or push" in prompt
    assert "do not stop to ask" in prompt.lower()


def test_a_claude_dir_path_is_detected_as_sensitive_block():
    blocked = r"C:\Users\someone\.claude\projects\repo\memory\notes.md"
    denials = [{"tool_name": "Edit", "tool_input": {"file_path": blocked}}]
    assert dispatch._detect_sensitive_paths(denials) == [blocked]


def test_an_ordinary_repo_path_is_an_ordinary_write():
    ok = r"C:\work\job-search-copilot\data\prep\acme.md"
    denials = [{"tool_name": "Edit", "tool_input": {"file_path": ok}}]
    assert dispatch._detect_sensitive_paths(denials) == []


def test_run_dispatch_sends_preamble_but_keeps_raw_request_for_log_and_job(tmp_path, monkeypatch):
    captured = {}

    class FakeCompleted:
        returncode = 0
        stdout = json.dumps({"result": "done: data/prep/acme.md written", "permission_denials": []}).encode("utf-8")
        stderr = b""

    def fake_run(cmd, **kwargs):
        captured["cmd"] = cmd
        return FakeCompleted()

    monkeypatch.setattr(dispatch.subprocess, "run", fake_run)
    monkeypatch.setattr(dispatch, "LOG_FILE", tmp_path / "dispatch-log.md")

    raw = "/job-apply --already-applied data/leads/0123456789ab.json"
    job_id = "test-job"
    dispatch._jobs[job_id] = {"status": "pending", "result": None, "request": raw}
    dispatch._run_dispatch(job_id, raw, tmp_path, None)

    assert captured["cmd"][:4] == ["claude", "-p", "--output-format", "json"]
    assert captured["cmd"][4] == dispatch.build_dispatch_prompt(raw)
    job = dispatch._jobs[job_id]
    assert job["status"] == "complete"
    assert job["request"] == raw                      # once-grant key + UI retry use the raw text
    log = (tmp_path / "dispatch-log.md").read_text(encoding="utf-8")
    assert raw[:100] in log
    assert "[Dispatch mode]" not in log                # preamble never pollutes the audit log


import threading


def _prep_tmp(tmp_path, monkeypatch, fake_run):
    monkeypatch.setattr(dispatch.subprocess, "run", fake_run)
    monkeypatch.setattr(dispatch, "LOG_FILE", tmp_path / "dispatch-log.md")


class _Ok:
    returncode = 0
    stdout = json.dumps({"result": "ok", "permission_denials": []}).encode("utf-8")
    stderr = b""


def test_run_dispatch_default_timeout_is_600(tmp_path, monkeypatch):
    seen = {}

    def fake_run(cmd, **kw):
        seen["timeout"] = kw["timeout"]
        return _Ok()
    _prep_tmp(tmp_path, monkeypatch, fake_run)
    dispatch._jobs["t600"] = {"status": "pending", "result": None, "request": "x"}
    dispatch._run_dispatch("t600", "x", tmp_path, None)
    assert seen["timeout"] == 600 and dispatch._jobs["t600"]["status"] == "complete"


def test_run_dispatch_honors_timeout_s(tmp_path, monkeypatch):
    seen = {}

    def fake_run(cmd, **kw):
        seen["timeout"] = kw["timeout"]
        return _Ok()
    _prep_tmp(tmp_path, monkeypatch, fake_run)
    dispatch._jobs["t1200"] = {"status": "pending", "result": None, "request": "x"}
    dispatch._run_dispatch("t1200", "x", tmp_path, None, timeout_s=1200)
    assert seen["timeout"] == 1200


def test_timeout_message_names_the_actual_limit(tmp_path, monkeypatch):
    def fake_run(cmd, **kw):
        raise dispatch.subprocess.TimeoutExpired(cmd, kw["timeout"])
    _prep_tmp(tmp_path, monkeypatch, fake_run)
    dispatch._jobs["tt"] = {"status": "pending", "result": None, "request": "x"}
    dispatch._run_dispatch("tt", "x", tmp_path, None, timeout_s=1200)
    assert dispatch._jobs["tt"]["status"] == "error"
    assert dispatch._jobs["tt"]["result"].startswith("Timed out after 20 minutes")


def test_create_dispatch_job_passes_timeout_through(tmp_path, monkeypatch):
    got = []
    done = threading.Event()

    def fake(job_id, request_text, repo_root, extra, timeout_s=600):
        got.append(timeout_s)
        if len(got) == 2:
            done.set()
    monkeypatch.setattr(dispatch, "_run_dispatch", fake)
    dispatch.create_dispatch_job("x", tmp_path, timeout_s=1200)
    dispatch.create_dispatch_job("y", tmp_path)
    assert done.wait(5) and sorted(got) == [600, 1200]


def test_disallowed_tools_flag_goes_after_the_prompt(tmp_path, monkeypatch):
    seen = {}

    def fake_run(cmd, **kw):
        seen["cmd"] = cmd
        return _Ok()
    _prep_tmp(tmp_path, monkeypatch, fake_run)
    dispatch._jobs["dd"] = {"status": "pending", "result": None, "request": "x"}
    dispatch._run_dispatch("dd", "x", tmp_path, ["A"], disallowed_tools=["B", "C"])
    cmd = seen["cmd"]
    assert cmd[-4:] == ["--allowed-tools", "A", "--disallowed-tools", "B,C"]
    assert cmd.index("--disallowed-tools") > 4  # after "claude -p --output-format json <prompt>"


def test_no_disallowed_tools_leaves_the_command_unchanged(tmp_path, monkeypatch):
    seen = {}

    def fake_run(cmd, **kw):
        seen["cmd"] = cmd
        return _Ok()
    _prep_tmp(tmp_path, monkeypatch, fake_run)
    dispatch._jobs["nd"] = {"status": "pending", "result": None, "request": "x"}
    dispatch._run_dispatch("nd", "x", tmp_path, None)
    assert "--disallowed-tools" not in seen["cmd"] and len(seen["cmd"]) == 5


def test_settings_flag_goes_after_the_prompt(tmp_path, monkeypatch):
    seen = {}

    def fake_run(cmd, **kw):
        seen["cmd"] = cmd
        return _Ok()
    _prep_tmp(tmp_path, monkeypatch, fake_run)
    dispatch._jobs["sp"] = {"status": "pending", "result": None, "request": "x"}
    dispatch._run_dispatch("sp", "x", tmp_path, None, disallowed_tools=["Bash", "WebFetch"],
                           settings_path="C:/x/settings.json")
    cmd = seen["cmd"]
    assert cmd[4] == dispatch.build_dispatch_prompt("x")
    assert cmd[-4:] == ["--disallowed-tools", "Bash,WebFetch", "--settings", "C:/x/settings.json"]
    assert cmd.index("--settings") > 4


def test_create_dispatch_job_passes_settings_path_through(tmp_path, monkeypatch):
    got = []
    done = threading.Event()

    def fake(job_id, request_text, repo_root, extra, timeout_s=600, **kw):
        got.append((timeout_s, kw))
        done.set()
    monkeypatch.setattr(dispatch, "_run_dispatch", fake)
    dispatch.create_dispatch_job("x", tmp_path, timeout_s=1200, disallowed_tools=["Bash"],
                                 settings_path="C:/s.json")
    assert done.wait(5)
    assert got == [(1200, {"disallowed_tools": ["Bash"], "settings_path": "C:/s.json"})]


def test_dispatch_env_strips_the_api_key_unless_told_to_keep_it(monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-test")
    monkeypatch.delenv("JSC_KEEP_API_KEY", raising=False)
    assert "ANTHROPIC_API_KEY" not in dispatch._subprocess_env()
    monkeypatch.setenv("JSC_KEEP_API_KEY", "1")
    assert dispatch._subprocess_env()["ANTHROPIC_API_KEY"] == "sk-test"


# --- Follow-ups 2026-10-15: a locked-down prep drafter that saved its pack is a success in the dispatch job list ---

def _denied(result, tools):
    class _R:
        returncode = 0
        stdout = json.dumps({"result": result, "permission_denials": [
            {"tool_name": t, "tool_use_id": f"u{i}", "tool_input": {"path": "C:/work/repo"}}
            for i, t in enumerate(tools)]}).encode("utf-8")
        stderr = b""
    return _R()


def _run_locked(tmp_path, monkeypatch, result, tools, pack_saved, job_id="pk"):
    dispatch._jobs.pop(job_id, None)  # module-level job list: start each case from a fresh id
    _prep_tmp(tmp_path, monkeypatch, lambda cmd, **kw: _denied(result, tools))
    monkeypatch.setattr(dispatch, "JOBS_FILE", tmp_path / "jobs.json")
    dispatch._jobs[job_id] = {"status": "pending", "result": None, "request": f"/job-apply --prep {job_id}"}
    dispatch._run_dispatch(job_id, f"/job-apply --prep {job_id}", tmp_path, None, disallowed_tools=["Bash"],
                           settings_path="C:/x/settings.json", pack_saved=pack_saved)
    log = (tmp_path / "dispatch-log.md").read_text(encoding="utf-8")
    saved = json.loads((tmp_path / "jobs.json").read_text(encoding="utf-8"))[job_id]
    return dispatch._jobs[job_id], log, saved


def test_locked_down_job_that_saved_its_pack_is_complete_with_blocked_tools(tmp_path, monkeypatch):
    job, log, saved = _run_locked(tmp_path, monkeypatch, "Pack ready.\nPREP_FILE: data/prep/x.md", ["Glob", "Grep", "Glob"],
                                  pack_saved=lambda r: "PREP_FILE" in r)
    assert job["status"] == "complete" and job["blockedTools"] == ["Glob", "Grep"]
    assert saved["status"] == "complete" and saved["blockedTools"] == ["Glob", "Grep"]
    assert "PERMISSION DENIED" not in log and "COMPLETE (locked-down drafter blocked from: Glob, Grep)" in log


def test_locked_down_job_without_a_pack_stays_permission_denied(tmp_path, monkeypatch):
    job, log, saved = _run_locked(tmp_path, monkeypatch, "I could not.", ["WebFetch"], pack_saved=lambda r: False,
                                  job_id="pk-none")
    assert job["status"] == "permission_denied" and job["deniedTools"] == ["WebFetch"]
    assert saved["status"] == "permission_denied" and "blockedTools" not in saved
    assert "PERMISSION DENIED — needs: WebFetch" in log


def test_a_pack_check_that_raises_falls_back_to_permission_denied(tmp_path, monkeypatch):
    def boom(_):
        raise RuntimeError("x")
    job, _, _ = _run_locked(tmp_path, monkeypatch, "PREP_FILE: data/prep/x.md", ["Glob"], pack_saved=boom, job_id="pk-boom")
    assert job["status"] == "permission_denied"


def test_denials_without_a_pack_check_are_unchanged(tmp_path, monkeypatch):
    # every non-prep dispatch: no pack_saved, so a denial is still a needs-permission result
    _prep_tmp(tmp_path, monkeypatch, lambda cmd, **kw: _denied("PREP_FILE: data/prep/x.md", ["Edit"]))
    dispatch._jobs["np"] = {"status": "pending", "result": None, "request": "np"}
    dispatch._run_dispatch("np", "np", tmp_path, None)
    assert dispatch._jobs["np"]["status"] == "permission_denied" and "blockedTools" not in dispatch._jobs["np"]


def test_load_jobs_restores_blocked_tools(tmp_path, monkeypatch):
    path = tmp_path / "jobs.json"
    path.write_text(json.dumps({"old-pk": {"status": "complete", "label": "l", "request": "r", "result": "ok",
                                           "started": 1.0, "blockedTools": ["Glob"]}}), encoding="utf-8")
    monkeypatch.setattr(dispatch, "JOBS_FILE", path)
    dispatch._jobs.pop("old-pk", None)
    dispatch.load_jobs()
    assert dispatch.get_dispatch_job("old-pk")["blockedTools"] == ["Glob"]


def test_create_dispatch_job_passes_pack_saved_through(tmp_path, monkeypatch):
    got = []
    done = threading.Event()

    def fake(job_id, request_text, repo_root, extra, timeout_s=600, **kw):
        got.append(kw)
        done.set()
    monkeypatch.setattr(dispatch, "_run_dispatch", fake)
    check = lambda r: True  # noqa: E731
    dispatch.create_dispatch_job("x", tmp_path, settings_path="C:/s.json", pack_saved=check)
    assert done.wait(5)
    assert got == [{"settings_path": "C:/s.json", "pack_saved": check}]
