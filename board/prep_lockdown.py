"""Per-job lockdown for the headless prep drafter (docs/ARCHITECTURE.md, "Form extractor and prep packs").

prepare() writes <data_dir>/.state/prep_lockdown/<lead>-<UTC stamp>/settings.json
— permissions.deny (DRAFTER_DENY) plus a PreToolUse hook on every tool (prep_write_hook.py, absolute interpreter) —
then self-tests the hook with the exact argv the settings command runs. Any failure raises LockdownError and the job
fails before dispatch. .claude/settings.json is never touched: deny rules in a --settings file win over its allows.
"""
import json
import os
import shutil
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

HOOK_PY = Path(__file__).resolve().parent / "prep_write_hook.py"
import sys as _sys  # noqa: E402
_ROOT = Path(__file__).resolve().parents[1]
if str(_ROOT) not in _sys.path:
    _sys.path.insert(0, str(_ROOT))
from copilot import config  # noqa: E402


def _rel(p: Path) -> str:
    try:
        return Path(p).resolve().relative_to(_ROOT.resolve()).as_posix()
    except ValueError:
        return Path(p).resolve().as_posix()


LOCKDOWN_DIR = _rel(config.state_dir() / "prep_lockdown")
PREP_REL = _rel(config.prep_dir())
HOOK_TIMEOUT_S = 30
LOCKDOWN_KEEP_DAYS = 7
SELF_TEST_TIMEOUT_S = 20
_AIRTABLE = "mcp__claude_ai_Airtable__"
# Tool names from .claude/settings.json and the connected MCP servers (read-only reference; never edited).
# A bare "mcp__<server>" rule covers every tool of that server.
DRAFTER_DENY = [
    "Bash", "PowerShell", "mcp__ide__executeCode", "WebFetch", "WebSearch", "Agent", "Task",
    "mcp__claude-in-chrome", "mcp__playwright",
    "mcp__claude_ai_Gmail", "mcp__claude_ai_Google_Calendar", "mcp__claude_ai_Zapier",
    "mcp__claude_ai_Google_Drive", "mcp__claude_ai_Claude_Docs", "mcp__plugin_github_github",
    # second layer if the hook ever fails open: tools that run code, message, schedule or leave the folder
    "Monitor", "NotebookEdit", "EnterWorktree", "ExitWorktree", "SendMessage", "CronCreate", "RemoteTrigger",
    "PushNotification",
] + [_AIRTABLE + n for n in (
    "create_automation", "create_base", "create_field", "create_interface", "create_page", "create_record_comment",
    "create_records_for_table", "create_table", "delete_automation", "delete_interface", "delete_page",
    "delete_records_for_table", "delete_table", "publish_interface", "revert_action", "submit_form",
    "test_automation_webhook_trigger", "update_automation", "update_field", "update_records_for_table",
    "update_table")]
SELF_TEST = (
    ({"tool_name": "Write", "tool_input": {"file_path": ".claude/settings.json", "content": "x"}}, 2),
    ({"tool_name": "Bash", "tool_input": {"command": "echo lockdown"}}, 2),
    ({"tool_name": "WebFetch", "tool_input": {"url": "https://example.com", "prompt": "x"}}, 2),
    ({"tool_name": "Write", "tool_input": {"file_path": PREP_REL + "/lockdown-self-test.md", "content": "x"}}, 0),
)


class LockdownError(Exception):
    pass


def _posix(p) -> str:
    return Path(p).as_posix()


def _under(root: Path, rel) -> Path:
    """A config path resolved against `root` (the repo the drafter runs in), unless it is absolute."""
    p = Path(str(rel))
    return p if p.is_absolute() else root / p


def read_args(root: Path, cfg: dict | None = None) -> list[str]:
    """--read-root / --read-file for the hook: the data dir, the configured writing-samples and contacts folders
    and the profile file. Folders inside the data dir are covered by it already; listing them again is harmless."""
    cfg = cfg if cfg is not None else config.load()
    ja = ((cfg.get("modules") or {}).get("job_apply") or {})
    data = os.environ.get("JSC_DATA_DIR") or cfg.get("data_dir") or "data"
    out = ["--read-root", _posix(_under(root, data))]
    for key in ("writing_samples_dir", "contacts_dir"):
        if ja.get(key):
            out += ["--read-root", _posix(_under(root, ja[key]))]
    if ja.get("profile_file"):
        out += ["--read-file", _posix(_under(root, ja["profile_file"]))]
    return out


def hook_argv(repo_root, cfg: dict | None = None) -> list[str]:
    root = Path(repo_root).resolve()
    return [_posix(sys.executable), _posix(HOOK_PY), "--prep-dir", _posix(root / PREP_REL), "--repo-root", _posix(root),
            *read_args(root, cfg)]


def hook_command(argv) -> str:
    return " ".join(f'"{a}"' for a in argv)


def build_settings(argv) -> dict:
    return {"permissions": {"deny": list(DRAFTER_DENY)},
            "hooks": {"PreToolUse": [{"matcher": "*", "hooks": [
                {"type": "command", "command": hook_command(argv), "timeout": HOOK_TIMEOUT_S}]}]}}


def self_test(argv, repo_root) -> list[str]:
    """Run the hook exactly as the CLI will. A hook that cannot run fails OPEN in Claude Code (only exit 2
    blocks), so a known-deny event must exit 2 and a known-allow event must exit 0 before anything dispatches.
    Runs the argv directly ("argv") and, when bash is available, the exact settings command string through
    `bash -c` ("shell": Claude Code runs hook commands through Git Bash on Windows). Returns the modes used."""
    cwd = str(Path(repo_root).resolve())
    runs = [("argv", list(argv))]
    bash = shutil.which("bash")
    if bash:
        runs.append(("shell", [bash, "-c", hook_command(argv)]))
    for mode, cmd in runs:
        for event, want in SELF_TEST:
            payload = json.dumps({**event, "hook_event_name": "PreToolUse", "cwd": cwd}).encode("utf-8")
            try:
                cp = subprocess.run(cmd, input=payload, capture_output=True, timeout=SELF_TEST_TIMEOUT_S)
            except (OSError, subprocess.SubprocessError) as e:
                raise LockdownError(f"lockdown self-test could not run the hook ({mode}): {e}") from e
            if cp.returncode != want:
                raise LockdownError(f"lockdown self-test failed ({mode}): {event['tool_name']} exited "
                                    f"{cp.returncode}, expected {want}")
    return [m for m, _ in runs]


def prune_old(repo_root, keep: Path, now: datetime) -> None:
    """Delete lockdown folders older than LOCKDOWN_KEEP_DAYS. Only real directories directly inside
    _prep_lockdown/ are touched (never files, symlinks or junctions, never `keep`); best effort."""
    base = (Path(repo_root).resolve() / LOCKDOWN_DIR)
    cutoff = now.timestamp() - LOCKDOWN_KEEP_DAYS * 86400
    try:
        entries = list(base.iterdir())
    except OSError:
        return
    for d in entries:
        try:
            if (d == keep or d.is_symlink() or getattr(d, "is_junction", lambda: False)() or not d.is_dir()
                    or d.resolve().parent != base.resolve() or d.stat().st_mtime >= cutoff):
                continue
            shutil.rmtree(d)
        except OSError:
            continue


def prepare(repo_root, lid: str, now: datetime) -> Path:
    root = Path(repo_root).resolve()
    stamp = now.astimezone(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    folder = root / LOCKDOWN_DIR / f"{lid}-{stamp}"
    folder.mkdir(parents=True, exist_ok=True)
    prune_old(root, keep=folder, now=now)
    argv = hook_argv(root)
    settings = build_settings(argv)
    path = folder / "settings.json"
    path.write_text(json.dumps(settings, indent=2, ensure_ascii=False), encoding="utf-8")
    if json.loads(path.read_text(encoding="utf-8")) != settings:
        raise LockdownError("lockdown settings did not round-trip")
    self_test(argv, root)
    return path
