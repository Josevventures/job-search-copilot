"""PreToolUse hook for the locked-down prep drafter (docs/ARCHITECTURE.md, "Form extractor and prep packs").

Run by the claude CLI before EVERY tool call of a `/job-apply --prep` dispatch (matcher "*", from the per-job
--settings file prep_lockdown.py writes). It is an allowlist and it fails closed: exit 0 allows, exit 2 denies
(reason on stderr), and any error at all denies. Writes: only .md files directly inside the Prep folder.
Reads: an allowlist of folders (read_roots_for), never secrets, never network paths. Stdlib only; run with an absolute interpreter path.
"""
import argparse
import json
import ntpath
import os
import re
import sys
from pathlib import Path

WRITE_TOOLS = {"Write": "file_path", "Edit": "file_path", "MultiEdit": "file_path"}  # NotebookEdit is denied
READ_TOOLS = {"Read": "file_path", "Grep": "path", "Glob": "path", "LS": "path"}
FREE_TOOLS = {"Skill", "ToolSearch", "TodoWrite",
              "mcp__claude_ai_Airtable__search_records", "mcp__claude_ai_Airtable__list_records_for_table",
              "mcp__claude_ai_Airtable__list_tables_for_base", "mcp__claude_ai_Airtable__get_table_schema",
              "mcp__claude_ai_Airtable__search_bases", "mcp__claude_ai_Airtable__list_bases"}
SECRET_DIRS = {".git", ".secrets", ".ssh"}
DEVICE_NAMES = {"CON", "PRN", "AUX", "NUL"} | {f"COM{i}" for i in range(1, 10)} | {f"LPT{i}" for i in range(1, 10)}
# Fail-closed Grep glob filter: any glob that mentions a secret-ish word or uses glob syntax that could spell one.
GLOB_DENY_WORDS = (".env", "secret", "credential", "token", ".pem", ".key", "settings.local", "{", "[", "..", "\\")


def read_roots_for(repo_root, home: Path, extra=()) -> list:
    """The only folders the drafter may read: the repo's skills (as shipped and as installed in .claude/skills),
    the user's skills/plugins, and the folders prep_lockdown passes with --read-root (the data dir: leads, prep,
    forms, writing samples; plus a contacts folder if configured elsewhere). Everything else in the repo (board/,
    docs/, .claude/ outside skills/, the repo root itself) is off limits."""
    repo = Path(repo_root)
    dirs = [repo / "skills", repo / ".claude" / "skills", home / "skills", home / "plugins", *map(Path, extra)]
    return [Path(os.path.realpath(d)) for d in dirs]


def read_files_for(files=()) -> list:
    """Exact files readable outside the folder allowlist (--read-file): the profile file the drafter reads for
    work authorization (/job-apply --prep), when it lives outside the data dir."""
    return [Path(os.path.realpath(Path(f))) for f in files]


def _key(p) -> str:
    return os.path.normcase(str(p)).rstrip("\\/")


def _network(raw) -> bool:
    """True for any UNC/device-style path. Windows treats a forward slash like a backslash, so a mixed-slash
    prefix is a UNC path and realpath on it opens an SMB connection (DNS/NTLM leak) before any deny. Rule,
    applied to the raw string before ANY filesystem call: the drive must be empty or a single drive letter,
    and there is no double-separator prefix in either slash direction."""
    if not isinstance(raw, str):
        return False
    t = raw.lstrip()
    if t.startswith(chr(92)) or t.startswith("//") or t.replace("/", chr(92)).startswith(chr(92) * 2):
        return True
    drive = ntpath.splitdrive(t)[0]
    return drive != "" and re.fullmatch(r"[A-Za-z]:", drive) is None


def _real(raw, cwd: str) -> Path | None:
    if not isinstance(raw, str) or not raw.strip() or "\x00" in raw or _network(raw):
        return None
    p = Path(raw)
    if ".." in p.parts:
        return None
    if not p.is_absolute():
        p = Path(cwd) / p
    return Path(os.path.realpath(p))


def _within(p: Path, root: Path) -> bool:
    k, r = _key(p), _key(root)
    return k == r or k.startswith(r + os.sep)


def _is_secret(p: Path) -> bool:
    n = p.name.lower()
    return (n.startswith(".env") or n.endswith(".env") or n == "settings.local.json"
            or n.endswith((".pem", ".key"))
            or (n.startswith("client_secret") and n.endswith(".json"))
            or (n.startswith("token") and n.endswith(".json"))
            or ("credentials" in n and n.endswith(".json"))
            or bool({part.lower() for part in p.parts} & SECRET_DIRS))


def _bad_glob_pattern(pat) -> bool:
    if not isinstance(pat, str) or not pat.strip():
        return True
    s = pat.strip()
    return (s[0] in "/\\" or (len(s) > 1 and s[1] == ":") or ".." in s or "\\" in s or "\x00" in s)


def _bad_grep_glob(g) -> bool:
    if g is None:
        return False
    if not isinstance(g, str):
        return True
    low = g.lower()
    return any(w in low for w in GLOB_DENY_WORDS)


def decide(event, prep_dir: Path, read_roots: list, read_files: list = ()) -> str | None:
    """None = allow; a string = the reason to deny."""
    if not isinstance(event, dict):
        return "event is not an object"
    tool, ti = event.get("tool_name"), event.get("tool_input")
    if not isinstance(tool, str) or not tool:
        return "no tool name"
    if not isinstance(ti, dict):
        return "tool_input is not an object"
    raw_cwd = event.get("cwd")
    if raw_cwd is not None and (not isinstance(raw_cwd, str) or _network(raw_cwd)):
        return "unsafe working directory"
    cwd = raw_cwd if raw_cwd else os.getcwd()
    if tool in WRITE_TOOLS:
        p = _real(ti.get(WRITE_TOOLS[tool]), cwd)
        if p is None:
            return f"{tool}: missing or unsafe path"
        if _key(p.parent) != _key(prep_dir):
            return f"{tool}: only files directly inside {prep_dir} may be written"
        name = p.name
        if not name.lower().endswith(".md") or ":" in name or name.startswith(".") or name != name.rstrip(" ."):
            return f"{tool}: only .md prep files may be written"
        if name.split(".")[0].strip().upper() in DEVICE_NAMES:
            return f"{tool}: reserved device name"
        return None
    if tool in READ_TOOLS:
        raw = ti.get(READ_TOOLS[tool])
        if tool == "Glob" and _network(ti.get("pattern")):
            return "Glob: unsafe pattern"
        if tool != "Read" and (not isinstance(raw, str) or not raw.strip()):
            return f"{tool}: a path inside the readable folders is required"
        p = _real(raw, cwd)
        if p is None:
            return f"{tool}: unsafe path"
        exact = tool == "Read" and any(_key(p) == _key(f) for f in read_files)
        if not exact and not any(_within(p, r) for r in read_roots):
            return f"{tool}: outside the readable folders"
        if _is_secret(p):
            return f"{tool}: secrets are off limits"
        if tool == "Grep" and _bad_grep_glob(ti.get("glob")):
            return "Grep: glob could select a secret"
        if tool == "Glob" and _bad_glob_pattern(ti.get("pattern")):
            return "Glob: unsafe pattern"
        return None
    if tool in FREE_TOOLS:
        return None
    return f"{tool} is not available to the locked-down prep drafter"


def main(argv=None, stdin_bytes=None) -> int:
    try:
        ap = argparse.ArgumentParser(add_help=False)
        ap.add_argument("--prep-dir", required=True)
        ap.add_argument("--repo-root", required=True)
        ap.add_argument("--read-root", action="append", default=[])
        ap.add_argument("--read-file", action="append", default=[])
        args = ap.parse_args(argv)
        raw = stdin_bytes if stdin_bytes is not None else sys.stdin.buffer.read()
        event = json.loads(raw.decode("utf-8"))
        roots = read_roots_for(os.path.realpath(args.repo_root), Path.home() / ".claude", args.read_root)
        reason = decide(event, Path(os.path.realpath(args.prep_dir)), roots, read_files_for(args.read_file))
    except BaseException as e:  # SystemExit from argparse included: any failure denies
        reason = f"hook error ({type(e).__name__})"
    if reason is None:
        return 0
    try:
        sys.stderr.write(f"prep lockdown: {reason}\n")
    except Exception:
        pass
    return 2


if __name__ == "__main__":
    sys.exit(main())
