"""
Dispatch layer (job_apply module) — runs a /job-apply request headlessly via the
`claude` CLI from the repo root, exactly as an interactive session would: same
CLAUDE.md, same .claude/settings.json permission allowlist and MCP connectors.
No bypass flag, no separate permission model.

Denial detection uses `claude -p --output-format json`, which returns a
structured `permission_denials` array (tool_name, tool_use_id, tool_input)
rather than free-form prose. This was a real, load-bearing fix made during
Task 8's own live verification: an earlier version of this file regex-matched
the model's final text summary for a denial phrase, and that phrase turned
out not to be deterministic across runs (one real dispatch said "permission
was not granted for `<tool>`", a second phrased it "permission for `<tool>`
wasn't granted" — the regex caught the first and silently missed the
second, misreporting a real denial as "complete"). Structured JSON output
does not have this problem.
"""
import subprocess
import threading
import uuid
import datetime
import json
import os
import sys
from pathlib import Path

import time as _time

_ROOT = Path(__file__).resolve().parents[1]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))
from copilot import config  # noqa: E402

# Parallel work (2026-10-08): at most MAX_PARALLEL jobs run at once, the rest wait as "queued";
# every job keeps its label; the job list is saved to a gitignored local file so a restart doesn't
# lose track of it (a job that was running at restart is shown as "interrupted").
MAX_PARALLEL = 3
_slots = threading.BoundedSemaphore(MAX_PARALLEL)
JOBS_FILE = config.state_dir() / "dispatch-jobs.json"
LOG_FILE = config.state_dir() / "dispatch-log.md"   # one line per outcome: the raw request, never the preamble
KEEP_JOBS = 50
_persist_lock = threading.Lock()


class _JobStore(dict):
    """Job states. Assigning a state merges into the job (so label and start time survive every status change)
    and saves the list."""

    def __setitem__(self, job_id, state):
        merged = {**self.get(job_id, {}), **state}
        super().__setitem__(job_id, merged)
        _persist()


_jobs: dict[str, dict] = _JobStore()
# Start time per job (issue 2026-10-08 #3): lets the board say how long a job has been running.
_started: dict[str, float] = {}


def _set(job_id: str, **fields) -> None:
    _jobs[job_id] = fields


def _persist() -> None:
    path = JOBS_FILE
    if path is None:
        return
    try:
        with _persist_lock:
            ids = list(_jobs)[-KEEP_JOBS:]
            data = {i: {"status": _jobs[i].get("status"), "label": _jobs[i].get("label"),
                        "request": str(_jobs[i].get("request") or "")[:300],
                        "result": None if _jobs[i].get("result") is None else str(_jobs[i]["result"])[:2000],
                        "started": _started.get(i),
                        **({"blockedTools": list(_jobs[i]["blockedTools"])} if _jobs[i].get("blockedTools") else {})}
                    for i in ids}
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
    except Exception:
        pass  # the record is a convenience; a failed save must never break a dispatch


def load_jobs() -> None:
    try:
        data = json.loads(Path(JOBS_FILE).read_text(encoding="utf-8"))
    except Exception:
        return
    for job_id, j in data.items():
        if job_id in _jobs:
            continue
        if j.get("status") in ("pending", "queued"):
            j = dict(j, status="interrupted",
                     result="The dashboard restarted while this job was running; check dispatch-log.md for its outcome.")
        if j.get("started"):
            _started[job_id] = j["started"]
        dict.__setitem__(_jobs, job_id, {k: v for k, v in j.items() if k != "started"})

# Loop-breaker for a real bug found live 2026-09-09: a request that edits a
# file under .claude/ (e.g. an agent definition under .claude/agents/)
# gets denied by the `claude` CLI's own built-in protection for that
# directory — a guardrail against an agent silently rewriting its own
# config/identity/permissions, completely independent of and un-liftable by
# anything in .claude/settings.json's permissions.allow list. Confirmed by
# direct reproduction: an Edit to a plain repo file with "Edit" in
# settings.json succeeded instantly with zero denial; the identical call
# against a file under .claude/ was denied with the model itself reporting
# "this file is flagged as sensitive." Before this fix, the dashboard had no
# way to tell this apart from an ordinary missing-permission denial, so it
# kept re-offering a Grant button that could never work — the same "Edit"
# permission was granted 15 times over 45 minutes on 2026-09-09 chasing a
# denial no grant could ever resolve.
_SENSITIVE_PATH_MARKER = ".claude" + os.sep
_SENSITIVE_TEXT_SIGNALS = ("flagged as sensitive", "sensitive file", "sensitive path")

# Tracks consecutive permission_denied results for the same (request text,
# denied-tools) pair, so a denial that recurs even after the tool was
# already granted gets flagged as structurally stuck rather than silently
# re-offering the same Grant button forever — a general safety net for any
# future case shaped like this one, not just the .claude/ case above.
_denial_repeat_counts: dict[tuple[str, tuple[str, ...]], int] = {}


# Headless-run preamble. Nobody is watching a dispatched run, so it must never stop to ask; and it must never
# push to git. The raw request text is still what gets logged and used as the once-grant correlation key; only
# the prompt sent to the CLI changes.
DISPATCH_PREAMBLE = (
    "[Dispatch mode] The user confirmed this request on the Jobs board and it is "
    "running headlessly through the claude CLI. Nobody is watching: do not stop to "
    "ask a question. If a detail is genuinely missing, complete everything that does "
    "not depend on it and state exactly what was skipped and why.\n\n"
    "Never write to any path containing `.claude\\` or `.claude/`: the CLI blocks those by "
    "design, and one such attempt makes this whole dispatch report as failed even "
    "when everything else succeeded.\n\n"
    "Do not commit or push to git. Finish with a short plain-text summary of what "
    "was done — file paths, record IDs, and anything skipped."
)


def build_dispatch_prompt(request_text: str) -> str:
    """The exact text handed to `claude -p`: the dispatch-mode preamble, then
    the confirmed request verbatim (last, so it ends the prompt)."""
    return f"{DISPATCH_PREAMBLE}\n\nRequest:\n{request_text}"


def _detect_sensitive_paths(denials: list[dict]) -> list[str]:
    paths = []
    for d in denials:
        file_path = (d.get("tool_input") or {}).get("file_path")
        if file_path and _SENSITIVE_PATH_MARKER in file_path:
            paths.append(file_path)
    return paths

# Accumulates which tools have been ONCE-granted for a given original
# request's text, across retries within that request's lifecycle. Fixes a
# real usability bug (2026-08-26): a task needing two sequential tool calls
# (e.g. a calendar search-then-create) required the user to click ONCE
# twice — the second retry, invoked fresh with only the newest tool
# allowed, immediately hit the SAME denial for the first tool again,
# because each retry is a brand-new `claude -p` process with no memory of
# what the previous attempt was already allowed to do. Keying by request
# text (not a job id) is deliberate: every retry of the same logical task
# reuses the exact same original request text unchanged (see server.py's
# grant_permission — extra_allowed_tools is always derived from
# body.originalRequest), so it's a reliable, simple correlation key without
# threading a new id through the whole grant/dispatch/frontend chain.
#
# Safety, thought through explicitly per the "verify this is actually
# clean" ask: still never touches .claude/settings.json (no persistence),
# and is cleared the moment that request's job reaches ANY terminal state
# (complete or error) — see the _clear_once_tools calls in
# _run_dispatch_inner — so it cannot outlive the one task the human was
# actually approving tool access for. It also only ever grows to include
# tools an actual attempted execution of that exact text genuinely asked
# for and was denied — never a preemptive/broad grant, and never anything
# beyond what the model itself decided the task needed while trying to
# fulfill that specific request. Known, accepted imperfection: if the exact
# same request text is dispatched again much later after an earlier
# attempt was abandoned without ever reaching a terminal state (every
# retry kept getting denied and the human gave up, or the backend
# restarted mid-task, which also wipes this in-memory dict), the
# accumulated tools would either be gone (backend restart) or could apply
# to a new, unrelated dispatch of byte-identical text (abandoned-without-
# resolving case). This does not grant anything the human didn't already
# explicitly approve once for that literal phrase — worst case it skips a
# repeat prompt for a tool already said yes to under identical wording — so
# it's flagged here as a known edge case, not a real vulnerability.
_once_tools_by_request: dict[str, set[str]] = {}


def accumulate_once_tool(request_text: str, tool: str) -> list[str]:
    """Adds `tool` to the running set of ONCE-approved tools for this exact
    request text and returns the full accumulated set (sorted, for a
    deterministic --allowed-tools string) — not just the one tool just
    granted."""
    tools = _once_tools_by_request.setdefault(request_text, set())
    tools.add(tool)
    return sorted(tools)


def _clear_once_tools(request_text: str):
    _once_tools_by_request.pop(request_text, None)
    for key in [k for k in _denial_repeat_counts if k[0] == request_text]:
        _denial_repeat_counts.pop(key, None)


def _subprocess_env() -> dict:
    """With a claude.ai login, ANTHROPIC_API_KEY in this process's environment would make the CLI switch to API-key
    auth, which disables the claude.ai OAuth-backed connectors (Airtable) — found live during the private build
    ("claude.ai connectors are disabled because ANTHROPIC_API_KEY ... takes precedence over your claude.ai login").
    So it is stripped before every call, unless JSC_KEEP_API_KEY=1 says the CLI is meant to run on the API key."""
    env = os.environ.copy()
    if os.environ.get("JSC_KEEP_API_KEY") != "1":
        env.pop("ANTHROPIC_API_KEY", None)
    return env


DEFAULT_TIMEOUT_S = 600  # per-job override: prep packs get 1200 s, Applied 1800 s (jobs.py)


def _note_result(repo_root, status: str, text: str) -> None:
    """Hook for post-processing a finished job's text. The private system scans it for facts to hold back from
    Airtable; this extraction keeps the call site and does nothing."""
    return None


def create_dispatch_job(request_text: str, repo_root: Path, extra_allowed_tools: list[str] | None = None,
                        timeout_s: int = DEFAULT_TIMEOUT_S, disallowed_tools: list[str] | None = None,
                        settings_path: str | None = None, label: str | None = None, pack_saved=None) -> str:
    job_id = str(uuid.uuid4())
    _started[job_id] = _time.time()
    _jobs[job_id] = {"status": "queued", "result": None, "request": request_text,
                     "label": label or request_text[:60]}
    kwargs = {}
    if disallowed_tools:
        kwargs["disallowed_tools"] = disallowed_tools
    if settings_path:
        kwargs["settings_path"] = settings_path
    if pack_saved is not None:
        kwargs["pack_saved"] = pack_saved
    thread = threading.Thread(
        target=_run_with_slot, args=(job_id, request_text, repo_root, extra_allowed_tools, timeout_s),
        kwargs=kwargs, daemon=True
    )
    thread.start()
    return job_id


def _run_with_slot(job_id, *args, **kwargs):
    slots = _slots
    slots.acquire()
    try:
        _set(job_id, status="pending")
        _run_dispatch(job_id, *args, **kwargs)
    finally:
        slots.release()


def list_jobs() -> list[tuple[str, dict]]:
    return [(i, get_dispatch_job(i)) for i in list(_jobs)]


def get_dispatch_job(job_id: str) -> dict | None:
    job = _jobs.get(job_id)
    if job is None:
        return None
    out = dict(job)
    t0 = _started.get(job_id)
    if t0 is not None:
        out["started"] = datetime.datetime.fromtimestamp(t0).isoformat(timespec="seconds")
        out["elapsedSeconds"] = int(_time.time() - t0)
    return out


def _append_dispatch_log(repo_root: Path, status_text: str, request_text: str):
    log_path = LOG_FILE
    log_path.parent.mkdir(parents=True, exist_ok=True)
    ts = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    short_req = request_text[:100].replace("\n", " ")
    line = f'- {ts} | {status_text} (request: "{short_req}")\n'
    with open(log_path, "a", encoding="utf-8") as f:
        f.write(line)


def _run_dispatch(job_id: str, request_text: str, repo_root: Path, extra_allowed_tools: list[str] | None,
                  timeout_s: int = DEFAULT_TIMEOUT_S, disallowed_tools: list[str] | None = None,
                  settings_path: str | None = None, pack_saved=None):
    _append_dispatch_log(repo_root, "DISPATCHED — running", request_text)
    cmd = ["claude", "-p", "--output-format", "json", build_dispatch_prompt(request_text)]
    if extra_allowed_tools:
        cmd += ["--allowed-tools", ",".join(extra_allowed_tools)]
    if disallowed_tools:  # after the prompt, like --allowed-tools: the variadic flag must not swallow it
        cmd += ["--disallowed-tools", ",".join(disallowed_tools)]
    if settings_path:  # prep drafter lockdown (prep-extractor 2026-10-01): per-job deny rules + PreToolUse hook
        cmd += ["--settings", settings_path]
    extra = {"pack_saved": pack_saved} if pack_saved is not None else {}  # non-prep calls keep their shape
    _run_dispatch_inner(job_id, request_text, repo_root, cmd, timeout_s, **extra)


def _saved_despite_denials(pack_saved, result_text: str) -> bool:
    """Prep drafter only (follow-ups 2026-10-15): the Jobs tab's own rule, "a saved pack is a success", handed in by
    jobs.py. The locked-down drafter can never be granted anything, so its denials are an audit detail, not a
    needs-permission state. Any error in the check means "not saved": the job stays permission_denied."""
    if pack_saved is None:
        return False
    try:
        return pack_saved(result_text) is True
    except Exception:
        return False


def _run_dispatch_inner(job_id: str, request_text: str, repo_root: Path, cmd: list[str],
                        timeout_s: int = DEFAULT_TIMEOUT_S, pack_saved=None):
    try:
        # Captured as raw bytes, not text=True. On this Windows/Python combo,
        # subprocess.run's text=True + encoding="utf-8" was empirically found
        # (during Task 8's own verification) to still mangle multi-byte UTF-8
        # characters like em-dashes, even though the child process's raw bytes
        # are genuinely valid UTF-8 (confirmed via hex inspection). Decoding
        # manually here sidesteps whatever internal path text=True was using.
        completed = subprocess.run(
            cmd, cwd=str(repo_root), capture_output=True, timeout=timeout_s, env=_subprocess_env(),
        )
        stdout_text = completed.stdout.decode("utf-8", errors="replace")
        stderr_text = completed.stderr.decode("utf-8", errors="replace")

        if completed.returncode != 0:
            stdout_stripped = stdout_text.strip()
            stderr_stripped = stderr_text.strip()
            # Bug fix (2026-08-26): this used to be `stderr_text.strip() or
            # f"exit code {completed.returncode}"` — when stderr was empty
            # (found live: a real failed dispatch had genuinely nothing on
            # stderr), the log and the chat reply both showed bare "exit
            # code 1" with zero diagnostic content, making the failure
            # undebuggable after the fact. claude -p's actual failure
            # explanation often lands on stdout (its normal output channel)
            # even on a non-zero exit — stderr is really only reserved for
            # process-level crashes. Capture whichever is non-empty,
            # preferring stderr as the stronger signal, but never silently
            # discard stdout.
            if stderr_stripped:
                diagnostic = stderr_stripped
            elif stdout_stripped:
                diagnostic = stdout_stripped
            else:
                diagnostic = "(no stderr or stdout captured)"
            err = f"exit code {completed.returncode}: {diagnostic}"
            _jobs[job_id] = {
                "status": "error", "result": err, "request": request_text,
                "stderr": stderr_stripped, "stdout": stdout_stripped,
            }
            _append_dispatch_log(repo_root, f"ERROR — {err[:500]}", request_text)
            _clear_once_tools(request_text)
            return

        try:
            parsed = json.loads(stdout_text)
        except json.JSONDecodeError:
            # Fall back to raw text if the CLI didn't emit valid JSON for some reason.
            raw = stdout_text.strip() or "(no output)"
            _jobs[job_id] = {"status": "complete", "result": raw, "request": request_text}
            _note_result(repo_root, "complete", raw)
            _append_dispatch_log(repo_root, f"COMPLETE (unparsed output) — {raw[:200]}", request_text)
            _clear_once_tools(request_text)
            return

        result_text = parsed.get("result", "").strip() or "(no output)"
        denials = parsed.get("permission_denials") or []
        if denials and _saved_despite_denials(pack_saved, result_text):
            blocked = list(dict.fromkeys(d["tool_name"] for d in denials))
            _jobs[job_id] = {"status": "complete", "result": result_text, "request": request_text,
                             "blockedTools": blocked}
            _note_result(repo_root, "complete", result_text)
            _append_dispatch_log(repo_root, f"COMPLETE (locked-down drafter blocked from: {', '.join(blocked)}) — "
                                            f"{result_text[:200]}", request_text)
            _clear_once_tools(request_text)
        elif denials:
            denied_tools = list(dict.fromkeys(d["tool_name"] for d in denials))  # dedupe, keep order
            sensitive_paths = _detect_sensitive_paths(denials)
            text_signals_sensitive = any(s in result_text.lower() for s in _SENSITIVE_TEXT_SIGNALS)
            sensitive_block = bool(sensitive_paths) or text_signals_sensitive

            repeat_key = (request_text, tuple(denied_tools))
            repeat_count = _denial_repeat_counts.get(repeat_key, 0) + 1
            _denial_repeat_counts[repeat_key] = repeat_count
            stuck = repeat_count >= 2  # denied again on the SAME tools after already having been granted once

            _jobs[job_id] = {
                "status": "permission_denied", "result": result_text,
                "request": request_text, "deniedTools": denied_tools,
                "sensitiveBlock": sensitive_block, "sensitivePaths": sensitive_paths,
                "stuck": stuck,
            }
            log_note = f"PERMISSION DENIED — needs: {', '.join(denied_tools)}"
            if sensitive_block:
                log_note += " (sensitive-path block — no grant can resolve this)"
            elif stuck:
                log_note += f" (repeat #{repeat_count} for this exact request — flagged stuck)"
            _append_dispatch_log(repo_root, log_note, request_text)
            # Deliberately NOT clearing _once_tools_by_request or
            # _denial_repeat_counts here — this is the one non-terminal
            # status, and the whole point of accumulate_once_tool (and the
            # repeat counter above) is that state from earlier in this same
            # request's retry chain carries forward to the next retry. See
            # the module-level comment on _once_tools_by_request.
        elif parsed.get("is_error"):
            _jobs[job_id] = {"status": "error", "result": result_text, "request": request_text}
            _append_dispatch_log(repo_root, f"ERROR — {result_text[:500]}", request_text)
            _clear_once_tools(request_text)
        else:
            _jobs[job_id] = {"status": "complete", "result": result_text, "request": request_text}
            _note_result(repo_root, "complete", result_text)
            _append_dispatch_log(repo_root, f"COMPLETE — {result_text[:200]}", request_text)
            _clear_once_tools(request_text)

    except subprocess.TimeoutExpired as e:
        # A timeout can still carry partial output captured before the kill
        # (subprocess.run populates it on the exception when capture_output
        # is set) — surface it instead of a bare "timed out" if present.
        partial = ""
        try:
            raw_partial = e.stdout or e.stderr
            if raw_partial:
                partial = raw_partial.decode("utf-8", errors="replace").strip()[:300]
        except Exception:
            pass
        msg = f"Timed out after {timeout_s // 60} minutes" + (f" — partial output: {partial}" if partial else "")
        _jobs[job_id] = {"status": "error", "result": msg, "request": request_text}
        _append_dispatch_log(repo_root, f"ERROR — {msg[:500]}", request_text)
        _clear_once_tools(request_text)
    except FileNotFoundError:
        _jobs[job_id] = {"status": "error", "result": "claude CLI not found on PATH", "request": request_text}
        _append_dispatch_log(repo_root, "ERROR — claude CLI not found", request_text)
        _clear_once_tools(request_text)


load_jobs()
