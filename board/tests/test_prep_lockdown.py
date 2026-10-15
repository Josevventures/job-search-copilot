"""Drafter lockdown (docs/ARCHITECTURE.md, "Form extractor and prep packs"): the per-job settings file,
the fail-closed PreToolUse hook and the pre-dispatch self-test.

Run from the repo root:  python -m pytest board/tests -q
"""
import json
import os
import shlex
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

import pytest

BACKEND = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BACKEND))

import jobs  # noqa: E402
import prep_lockdown  # noqa: E402
import prep_write_hook  # noqa: E402

PREP = prep_lockdown.PREP_REL
BS = chr(92)
UNC = BS * 2 + "192.0.2.1" + BS + "s" + BS + "x.md"  # a network path: realpath on it opens an SMB connection
NOW = datetime(2026, 10, 1, 12, 1, 30, tzinfo=timezone.utc)
# The config the hook's read allowlist is built from in these tests: the data dir plus a profile file outside it.
CFG = {"data_dir": "data", "modules": {"job_apply": {"profile_file": "notes/profile.md",
                                                     "writing_samples_dir": "data/writing-samples"}}}


@pytest.fixture
def repo(tmp_path):
    r = (tmp_path / "repo").resolve()
    (r / PREP / "_forms").mkdir(parents=True)
    return r


def decide(repo, tool, **tool_input):
    args = ["--prep-dir", str(repo / PREP), "--repo-root", str(repo), *prep_lockdown.read_args(repo, CFG)]
    event = {"hook_event_name": "PreToolUse", "cwd": str(repo), "tool_name": tool, "tool_input": tool_input}
    return prep_write_hook.main(args, json.dumps(event).encode("utf-8"))


def test_prep_dir_matches_jobs():
    assert jobs.PREP_DIR == PREP + "/"


@pytest.mark.parametrize("tool", ["Write", "Edit", "MultiEdit"])
@pytest.mark.parametrize("name", ["acme-2026-10-01.md", "acme-2026-10-01.draft.md", "acme-2026-10-01-2.md"])
def test_writes_directly_in_prep_are_allowed(repo, tool, name):
    assert decide(repo, tool, file_path=f"{PREP}/{name}") == 0
    assert decide(repo, tool, file_path=str(repo / PREP / name)) == 0
    assert decide(repo, tool, file_path=str(repo / PREP / name).replace("\\", "/")) == 0


def test_reading_the_resume_pdf_is_allowed(repo):
    # The drafter takes resume wording from the approved resume PDF in the data dir.
    pdf = "data/resumes/Resume 2026-09-29.pdf"
    assert decide(repo, "Read", file_path=pdf) == 0
    assert decide(repo, "Read", file_path=str(repo / pdf)) == 0


@pytest.mark.skipif(os.name != "nt", reason="case-insensitive paths are a Windows property")
def test_case_variants_of_the_prep_dir_are_the_same_folder(repo):
    assert decide(repo, "Write", file_path="DATA/Prep/Acme.MD") == 0


@pytest.mark.parametrize("tool,tool_input", [
    ("Write", {"file_path": "notes/other.md"}),
    ("Write", {"file_path": ".claude/settings.json"}),
    ("Edit", {"file_path": ".claude/settings.json"}),
    ("MultiEdit", {"file_path": ".claude/skills/job-apply/SKILL.md"}),
    ("Write", {"file_path": f"{PREP}/../../../../notes/x.md"}),
    ("Write", {"file_path": f"{PREP}/x.json"}),
    ("Write", {"file_path": f"{PREP}/CON.md"}),
    ("Write", {"file_path": f"{PREP}/nul.md"}),
    ("Write", {"file_path": f"{PREP}/com1.md"}),
    ("Write", {"file_path": f"{PREP}/LPT9.draft.md"}),
    ("NotebookEdit", {"notebook_path": f"{PREP}/x.md"}),
    ("Read", {"file_path": "docs/x.md"}),
    ("Read", {"file_path": ".claude/settings.local.json"}),
    ("Read", {"file_path": "data/.env.local"}),
    ("Read", {"file_path": "data/.env.example"}),
    ("Read", {"file_path": "data/settings.local.json"}),
    ("Read", {"file_path": "data/client_secret_123.json"}),
    ("Read", {"file_path": "data/id.pem"}),
    ("Read", {"file_path": "data/server.key"}),
    ("Read", {"file_path": "data/token_abc.json"}),
    ("Read", {"file_path": "data/my-credentials.json"}),
    ("Grep", {"pattern": "x"}),
    ("Grep", {"pattern": "x", "path": "board"}),
    ("Grep", {"pattern": "x", "path": "data", "glob": ".env"}),
    ("Grep", {"pattern": "x", "path": "data", "glob": "**/settings.local.json"}),
    ("Grep", {"pattern": "x", "path": "data", "glob": "*.pem"}),
    ("Glob", {"pattern": "**/.env"}),
    ("Glob", {"pattern": "C:/Users/**"}),
    ("Glob", {"pattern": "/etc/*", "path": "data"}),
    ("Glob", {"pattern": "../*", "path": "data"}),
    ("Glob", {"pattern": "a" + chr(92) + "b", "path": "data"}),
    ("Glob", {"pattern": "*.md", "path": "board"}),
    ("Read", {"file_path": "//192.0.2.1/s/x.md"}),
    ("Write", {"file_path": UNC}),
    ("Read", {"file_path": BS * 2 + "?" + BS + "C:" + BS + "x.md"}),
    ("Write", {"file_path": f"{PREP}/x.md.txt"}),
    ("Write", {"file_path": f"{PREP}/_forms/x.md"}),
    ("Write", {"file_path": f"{PREP}/evil:x.md"}),
    ("Write", {"file_path": f"{PREP}/.hidden.md"}),
    ("Write", {"file_path": "C:/Windows/Temp/x.md"}),
    ("Write", {"file_path": ""}),
    ("Write", {"file_path": 123}),
    ("Write", {}),
    ("NotebookEdit", {"notebook_path": f"{PREP}/x.ipynb"}),
    ("Bash", {"command": "echo hi"}),
    ("PowerShell", {"command": "dir"}),
    ("WebFetch", {"url": "https://example.com", "prompt": "x"}),
    ("WebSearch", {"query": "x"}),
    ("Agent", {"prompt": "x"}),
    ("Task", {"prompt": "x"}),
    ("mcp__ide__executeCode", {"code": "1"}),
    ("mcp__claude-in-chrome__navigate", {"url": "https://example.com"}),
    ("mcp__playwright__playwright_navigate", {"url": "https://example.com"}),
    ("mcp__claude_ai_Gmail__send_message", {}),
    ("mcp__claude_ai_Airtable__create_records_for_table", {}),
    ("mcp__claude_ai_Zapier__execute_zapier_write_action", {}),
    ("mcp__claude_ai_Google_Calendar__create_event", {}),
    ("mcp__some_new_server__do_anything", {}),
    ("Read", {"file_path": ".env"}),
    ("Read", {"file_path": "board/token.json"}),
    ("Read", {"file_path": "data/.secrets/credentials.json"}),
    ("Read", {"file_path": ".git/config"}),
    ("Read", {"file_path": "C:/Windows/win.ini"}),
    ("Read", {"file_path": "data/../../outside.txt"}),
    ("Grep", {"pattern": "x", "path": "C:/Windows"}),
])
def test_everything_else_is_denied(repo, tool, tool_input):
    assert decide(repo, tool, **tool_input) == 2


@pytest.mark.parametrize("tool,tool_input", [
    ("Read", {"file_path": "data/leads/abcdefabcdef.json"}),
    ("Read", {"file_path": f"{PREP}/_forms/abcdefabcdef-20261001T120130Z.json"}),
    ("Read", {"file_path": ".claude/skills/job-apply/SKILL.md"}),
    ("Grep", {"pattern": "Acme", "path": "data/contacts"}),
    ("Grep", {"pattern": "Acme", "path": "data/contacts", "glob": "*.md"}),
    ("Glob", {"pattern": "**/*.md", "path": "data"}),
    ("Glob", {"pattern": "*.md", "path": "data/writing-samples"}),
    ("Read", {"file_path": "skills/job-apply/SKILL.md"}),
    ("Skill", {"skill": "humanizer"}),
    ("ToolSearch", {"query": "select:mcp__claude_ai_Airtable__search_records"}),
    ("TodoWrite", {"todos": []}),
    ("mcp__claude_ai_Airtable__search_records", {"baseId": "appBASE"}),
    ("mcp__claude_ai_Airtable__list_records_for_table", {}),
])
def test_reads_and_local_tools_are_allowed(repo, tool, tool_input):
    assert decide(repo, tool, **tool_input) == 0


def test_skill_reference_files_under_home_claude_are_readable(repo):
    path = Path.home() / ".claude" / "skills" / "humanizer" / "SKILL.md"
    assert decide(repo, "Read", file_path=str(path)) == 0


@pytest.mark.parametrize("raw", [b"", b"not json", b"[1, 2]", b"\xff\xfe",
                                 json.dumps({"tool_name": "Write", "tool_input": []}).encode("utf-8"),
                                 json.dumps({"tool_input": {}}).encode("utf-8")])
def test_malformed_events_are_denied(repo, raw):
    assert prep_write_hook.main(["--prep-dir", str(repo / PREP), "--repo-root", str(repo)], raw) == 2


def test_missing_arguments_deny():
    assert prep_write_hook.main([], b'{"tool_name": "Read", "tool_input": {}}') == 2


def _run(argv, repo, event):
    payload = {"hook_event_name": "PreToolUse", "cwd": str(repo), **event}
    return subprocess.run(argv, input=json.dumps(payload).encode("utf-8"), capture_output=True, timeout=30)


def test_hook_as_a_subprocess_uses_exit_codes(repo):
    argv = prep_lockdown.hook_argv(repo)
    ok = _run(argv, repo, {"tool_name": "Write", "tool_input": {"file_path": f"{PREP}/a.md"}})
    bad = _run(argv, repo, {"tool_name": "Write", "tool_input": {"file_path": "notes/a.md"}})
    assert ok.returncode == 0 and bad.returncode == 2
    assert b"prep lockdown:" in bad.stderr


def test_prepare_writes_settings_with_the_deny_list_and_the_hook(repo):
    path = prep_lockdown.prepare(repo, "abcdefabcdef", NOW)
    assert path == repo / prep_lockdown.LOCKDOWN_DIR / "abcdefabcdef-20261001T120130Z" / "settings.json"
    s = json.loads(path.read_text(encoding="utf-8"))
    assert s["permissions"] == {"deny": prep_lockdown.DRAFTER_DENY}
    [entry] = s["hooks"]["PreToolUse"]
    assert entry["matcher"] == "*"
    argv = prep_lockdown.hook_argv(repo)
    assert entry["hooks"] == [{"type": "command", "command": prep_lockdown.hook_command(argv), "timeout": 30}]
    assert shlex.split(entry["hooks"][0]["command"]) == argv
    assert Path(argv[0]).is_absolute() and Path(argv[0]).name.lower().startswith("python")
    assert Path(argv[1]).resolve() == (BACKEND / "prep_write_hook.py").resolve()
    assert not (repo / ".claude").exists()


def test_deny_list_covers_every_dangerous_surface():
    deny = set(prep_lockdown.DRAFTER_DENY)
    for t in ("Bash", "PowerShell", "mcp__ide__executeCode", "WebFetch", "WebSearch", "Agent", "Task",
              "mcp__claude-in-chrome", "mcp__playwright", "mcp__claude_ai_Gmail", "mcp__claude_ai_Google_Calendar",
              "mcp__claude_ai_Zapier", "mcp__claude_ai_Airtable__create_records_for_table",
              "mcp__claude_ai_Airtable__update_records_for_table", "mcp__claude_ai_Airtable__delete_records_for_table",
              "Monitor", "NotebookEdit", "EnterWorktree", "ExitWorktree", "SendMessage", "CronCreate",
              "RemoteTrigger", "PushNotification"):
        assert t in deny, t
    reads = {"search_records", "list_records_for_table", "list_tables_for_base", "get_table_schema",
             "search_bases", "list_bases"}
    assert not {t for t in deny if t.startswith("mcp__claude_ai_Airtable__") and t.rsplit("__", 1)[1] in reads}


def test_hook_allowlist_and_deny_list_never_overlap():
    allowed = set(prep_write_hook.FREE_TOOLS) | set(prep_write_hook.READ_TOOLS) | set(prep_write_hook.WRITE_TOOLS)
    assert not allowed & set(prep_lockdown.DRAFTER_DENY)


def test_self_test_passes_with_the_real_hook(repo):
    prep_lockdown.self_test(prep_lockdown.hook_argv(repo), repo)


def test_self_test_fails_with_a_missing_interpreter(repo, tmp_path):
    argv = prep_lockdown.hook_argv(repo)
    argv[0] = (tmp_path / "missing" / "python.exe").as_posix()
    with pytest.raises(prep_lockdown.LockdownError, match="could not run the hook"):
        prep_lockdown.self_test(argv, repo)


def test_self_test_fails_with_an_allow_everything_hook(repo):
    with pytest.raises(prep_lockdown.LockdownError, match="expected 2"):
        prep_lockdown.self_test([sys.executable, "-c", "import sys; sys.exit(0)"], repo)


def test_self_test_fails_with_a_deny_everything_hook(repo):
    with pytest.raises(prep_lockdown.LockdownError, match="expected 0"):
        prep_lockdown.self_test([sys.executable, "-c", "import sys; sys.exit(2)"], repo)


def test_prepare_fails_closed_when_the_hook_script_is_missing(repo, monkeypatch):
    monkeypatch.setattr(prep_lockdown, "HOOK_PY", repo / "nope.py")
    with pytest.raises(prep_lockdown.LockdownError):
        prep_lockdown.prepare(repo, "abcdefabcdef", NOW)


def test_unc_paths_are_denied_fast_without_touching_the_network(repo):
    import time
    t = time.monotonic()
    for tool, key in (("Read", "file_path"), ("Write", "file_path"), ("Grep", "path"), ("Glob", "path")):
        assert decide(repo, tool, **{key: UNC, "pattern": "x"}) == 2
    assert time.monotonic() - t < 1


def test_unc_cwd_is_denied_fast(repo):
    import time
    event = {"tool_name": "Read", "cwd": UNC, "tool_input": {"file_path": "data/x.md"}}
    t = time.monotonic()
    assert prep_write_hook.main(["--prep-dir", str(repo / PREP), "--repo-root", str(repo)],
                                json.dumps(event).encode("utf-8")) == 2
    assert time.monotonic() - t < 1


def test_self_test_also_runs_the_settings_command_string(repo):
    modes = prep_lockdown.self_test(prep_lockdown.hook_argv(repo), repo)
    assert "argv" in modes
    import shutil
    if shutil.which("bash"):
        assert "shell" in modes


def test_self_test_fails_when_the_command_string_fails_in_the_shell(repo, monkeypatch):
    import shutil
    if not shutil.which("bash"):
        pytest.skip("no bash")
    monkeypatch.setattr(prep_lockdown, "hook_command", lambda argv: "exit 0")
    with pytest.raises(prep_lockdown.LockdownError, match="expected 2"):
        prep_lockdown.self_test(prep_lockdown.hook_argv(repo), repo)


def test_exactly_one_profile_file_outside_the_data_dir_is_readable(repo):
    # Work authorization comes from the profile file (--read-file); nothing else in its folder.
    (repo / "notes").mkdir()
    assert decide(repo, "Read", file_path="notes/profile.md") == 0
    assert decide(repo, "Read", file_path=str(repo / "notes" / "profile.md")) == 0
    for neighbour in ("notes/other.md", "notes/x.md", "notes/profile.md.bak",
                      "notes/sub/profile.md", "notes/../notes/profile.md",
                      "notes", "board/profile.md"):
        assert decide(repo, "Read", file_path=neighbour) == 2, neighbour
    assert decide(repo, "Grep", pattern="x", path="notes") == 2
    assert decide(repo, "Grep", pattern="x", path="notes/profile.md") == 2
    assert decide(repo, "Glob", pattern="*.md", path="notes") == 2
    assert decide(repo, "Write", file_path="notes/profile.md") == 2


@pytest.mark.skipif(os.name != "nt", reason="case-insensitive paths are a Windows property")
def test_the_profile_file_allow_is_case_insensitive_on_windows(repo):
    assert decide(repo, "Read", file_path=str(repo / "NOTES" / "Profile.MD")) == 0


# ---- mixed-slash UNC bypass (final whole-branch review): NO network-style path may reach any filesystem call ----
MIXED_NETWORK_PATHS = [
    "/" + BS + "host" + BS + "share" + BS + "x.md",
    "/" + BS + "host/share/x.md",
    "/" + BS + "?" + BS + "UNC" + BS + "host" + BS + "share" + BS + "x",
    "/" + BS + "?" + BS + "C:" + BS + "x",
    BS + "/host" + BS + "share" + BS + "x.md",
    " /" + BS + "host" + BS + "s" + BS + "x",
]


def _spy_filesystem(monkeypatch):
    """Any realpath call the hook makes is recorded; a network path must produce none."""
    import time
    calls = []

    def spy(p, *a, **kw):
        calls.append(str(p))
        return str(p)
    monkeypatch.setattr(prep_write_hook.os.path, "realpath", spy)
    monkeypatch.setattr(prep_write_hook.os, "getcwd", lambda: "C:" + BS + "safe")
    return calls, time.monotonic


def _decide_raw(tool, cwd="C:" + BS + "safe", **tool_input):
    event = {"hook_event_name": "PreToolUse", "cwd": cwd, "tool_name": tool, "tool_input": tool_input}
    return prep_write_hook.decide(event, Path("C:" + BS + "safe" + BS + "prep"), [])


@pytest.mark.parametrize("bad", MIXED_NETWORK_PATHS)
@pytest.mark.parametrize("tool,key,extra", [
    ("Read", "file_path", {}), ("Write", "file_path", {"content": "x"}), ("Edit", "file_path", {}),
    ("MultiEdit", "file_path", {}), ("Grep", "path", {"pattern": "x"}), ("Glob", "path", {"pattern": "*.md"}),
    ("LS", "path", {}),
])
def test_mixed_slash_network_paths_denied_without_touching_the_filesystem(monkeypatch, tool, key, extra, bad):
    calls, now = _spy_filesystem(monkeypatch)
    t0 = now()
    reason = _decide_raw(tool, **{key: bad}, **extra)
    assert reason is not None
    assert calls == []
    assert now() - t0 < 1


@pytest.mark.parametrize("bad", MIXED_NETWORK_PATHS)
def test_mixed_slash_network_glob_pattern_denied_without_touching_the_filesystem(monkeypatch, bad):
    calls, now = _spy_filesystem(monkeypatch)
    t0 = now()
    reason = _decide_raw("Glob", path="C:" + BS + "safe", pattern=bad)
    assert reason is not None
    assert calls == []
    assert now() - t0 < 1


@pytest.mark.parametrize("bad", MIXED_NETWORK_PATHS)
@pytest.mark.parametrize("tool,key,extra", [
    ("Read", "file_path", {}), ("Write", "file_path", {"content": "x"}), ("Grep", "path", {"pattern": "x"}),
])
def test_mixed_slash_network_cwd_denied_without_touching_the_filesystem(monkeypatch, tool, key, extra, bad):
    calls, now = _spy_filesystem(monkeypatch)
    t0 = now()
    reason = _decide_raw(tool, cwd=bad, **{key: "data/x.md"}, **extra)
    assert reason is not None
    assert calls == []
    assert now() - t0 < 1


def test_drive_letter_and_relative_paths_still_reach_realpath(monkeypatch):
    seen = []
    monkeypatch.setattr(prep_write_hook.os.path, "realpath", lambda p: seen.append(str(p)) or str(p))
    _decide_raw("Read", file_path="C:" + BS + "data" + BS + "x.md")
    _decide_raw("Read", file_path="data/x.md")
    assert len(seen) == 2


def test_prepare_deletes_old_lockdown_folders_but_not_recent_or_the_new_one(repo):
    base = repo / prep_lockdown.LOCKDOWN_DIR
    old, recent = base / "aaaaaaaaaaaa-20260901T000000Z", base / "bbbbbbbbbbbb-20260930T000000Z"
    for d in (old, recent):
        d.mkdir(parents=True)
        (d / "settings.json").write_text("{}", encoding="utf-8")
    stale = NOW.timestamp() - 8 * 86400
    os.utime(old, (stale, stale))
    path = prep_lockdown.prepare(repo, "abcdefabcdef", NOW)
    assert not old.exists()
    assert recent.exists()
    assert path.exists()


def test_cleanup_never_removes_the_folder_being_created_even_if_old(repo):
    base = repo / prep_lockdown.LOCKDOWN_DIR
    folder = base / "abcdefabcdef-20261001T120130Z"
    folder.mkdir(parents=True)
    stale = NOW.timestamp() - 30 * 86400
    os.utime(folder, (stale, stale))
    prep_lockdown.prune_old(repo, keep=folder, now=NOW)
    assert folder.exists()


def test_cleanup_ignores_files_and_links_pointing_outside(repo, tmp_path):
    base = repo / prep_lockdown.LOCKDOWN_DIR
    base.mkdir(parents=True)
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "keep.txt").write_text("x", encoding="utf-8")
    (base / "stray.txt").write_text("x", encoding="utf-8")
    link = base / "link-20200101T000000Z"
    try:
        os.symlink(outside, link, target_is_directory=True)
    except (OSError, NotImplementedError):
        pytest.skip("symlinks unavailable")
    stale = NOW.timestamp() - 30 * 86400
    os.utime(base / "stray.txt", (stale, stale))
    if os.utime in os.supports_follow_symlinks:
        os.utime(link, (stale, stale), follow_symlinks=False)
    else:  # Windows: age the target instead; prune_old's stat() follows the link, so the link still reads stale
        os.utime(outside, (stale, stale))
    prep_lockdown.prune_old(repo, keep=base / "none", now=NOW)
    assert (outside / "keep.txt").exists()
    assert (base / "stray.txt").exists()


# --- Follow-ups 2026-10-15: the job-apply --prep instructions must only send the drafter where the hook allows ---

def _prep_skill_section() -> str:
    import re
    text = (jobs.REPO_ROOT / "skills" / "job-apply" / "SKILL.md").read_text(encoding="utf-8")
    return re.search(r"### `--prep <lead file> --form <form_fields\.json>`(.*?)### `--already-applied", text, re.S).group(1)


def test_prep_skill_names_the_profile_file_with_the_read_tool(repo):
    sec = _prep_skill_section()
    assert "Read the profile file" in sec and "never Grep" in sec
    assert decide(repo, "Read", file_path="notes/profile.md") == 0
    # Acceptance 2026-10-15: a drafter Grepped the right file and was refused (the exemption is Read-only)
    assert decide(repo, "Grep", pattern="visa", path=str(repo / "notes" / "profile.md")) == 2


def test_prep_skill_read_folders_are_inside_the_hook_allowlist(repo):
    import re
    # every folder the section names, except on the line that lists what is off limits
    sec = "\n".join(line for line in _prep_skill_section().splitlines() if "Off limits" not in line)
    folders = set(re.findall(r"`((?:data|skills|\.claude)/(?:[^`*<>]*/)?)`", sec))
    assert {"data/", "skills/", ".claude/skills/"} <= folders, folders
    for f in sorted(folders):
        assert decide(repo, "Glob", pattern="*.md", path=f.rstrip("/")) == 0, f
        assert decide(repo, "Grep", pattern="x", path=f.rstrip("/")) == 0, f


def test_acceptance_run_denials_stay_denied_and_the_prescribed_forms_are_allowed(repo):
    # The shapes of the calls the three 2026-10-15 acceptance drafters made that the hook refused; none is loosened.
    assert decide(repo, "Glob", pattern="data/resumes/*", path=str(repo)) == 2
    assert decide(repo, "Glob", pattern="data/prep/*.md") == 2   # no path: repo root
    assert decide(repo, "Grep", pattern="x", path=str(repo / "notes")) == 2
    assert decide(repo, "Read", file_path=str(repo / "notes" / "other-project.md")) == 2
    assert decide(repo, "Grep", pattern="x", path=str(repo / "docs" / "SETUP.md")) == 2
    spill = Path.home() / ".claude" / "projects" / "some-repo" / "s" / "tool-results" / "x.txt"
    assert decide(repo, "Grep", pattern="Acme", path=str(spill)) == 2
    # What the skill prescribes instead
    assert decide(repo, "Glob", pattern="*.pdf", path="data/resumes") == 0
    assert decide(repo, "Glob", pattern="*.md", path=PREP) == 0
    assert decide(repo, "mcp__claude_ai_Airtable__get_table_schema", baseId="app", tables=[]) == 0
