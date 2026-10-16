"""Jobs board backend (design: docs/ARCHITECTURE.md, "The board"; decisions: docs/DESIGN-DECISIONS.md).

Prep is two steps (prep-extractor addendum): the form extractor subprocess, then a locked-down drafter.
The only writer of a lead file's `decision` group (spec §4 single-writer rule). Every
action is first recorded as PENDING for UNDO_WINDOW_S seconds; the sweeper
(start_sweeper) executes due actions. Dispatched jobs never edit lead files: their
results come back as marker lines in the job output.

Stdlib + fastapi/pydantic only. lead_store is imported from the job-apply-fleet skill, which is stdlib-only by
design. Applied / prep / terminal dispatch needs modules.job_apply; without it those actions answer 503 and the
board still ranks, skips, moves and undoes.
"""
import hashlib
import json
import math
import os
import re
import subprocess
import sys
import threading
import warnings
from datetime import datetime, timedelta, timezone
from pathlib import Path

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

REPO_ROOT = Path(__file__).resolve().parents[1]  # board -> repo
SKILL_DIR = REPO_ROOT / "skills" / "job-apply-fleet"
for _p in (REPO_ROOT, SKILL_DIR):
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))

from copilot import config  # noqa: E402

from lib.routing import warm_current_contact  # noqa: E402
from lib.lead_store import LeadStore, LeadStoreError  # noqa: E402
import prep_lockdown  # noqa: E402  (stdlib-only; Backend/ is on sys.path for server.py and the tests)
from prep_extract.schema import SCHEMA_VERSION as FORM_SCHEMA_VERSION, stub_result  # noqa: E402  (stdlib-only)

UNDO_WINDOW_S = 90
SWEEP_INTERVAL_S = 5
ACTIONS = ("skip", "applied", "prep", "terminal", "move_up", "move_down", "reset_band", "still_open")
MOVE_ACTIONS = ("move_up", "move_down")
BAND_ACTIONS = ("move_up", "move_down", "reset_band")
BAND_KEYS = ("apply_now", "worth_a_look", "low")
JOB_ACTIONS = ("applied", "prep", "terminal")  # each sets job_status: never while a job is running
SKIP_REASONS = ("wrong_level", "wrong_function", "location", "pay", "company_industry", "closed_stale",
                "not_interested")
NOTE_MAX = 500
LEAD_ID_RE = re.compile(r"[0-9a-f]{12}")
TRIAGE_VIEW_KEYS = ("company", "role_title", "location", "pay", "pay_type", "jd_url", "jd_read", "posting_status",
                    "fit_score", "fit_rationale", "network", "engage", "airtable_match", "eligibility_note",
                    "triage_status", "triage_error", "triaged_at", "route", "job_summary", "scores", "main_gap", "level",
                    "locations", "modality", "relocation_assistance", "pay_bands", "pay_source", "bonus", "equity",
                    "posted_date", "posted_date_source", "reposted", "applicant_count", "inbound", "recruiter_signal",
                    # v1 leads only (Needs re-triage ordering and the old Why line); null on v2
                    "category", "relevance", "why", "bucket", "bucket_reason")
# Everything scoring_bridge.lead_to_view + jobs_scoring.js read from triage, so a stored decision can be re-scored
# later (ranking-engine spec D8). Raw inputs only: no Python scoring (the chosen band / verified flag stay in JS).
SNAPSHOT_INPUT_KEYS = ("scores", "level", "pay_type", "pay_bands", "pay_source", "bonus", "equity", "locations",
                       "modality", "posted_date", "reposted", "applicant_count", "inbound", "route",
                       "network", "airtable_match", "relocation_assistance", "posting_status", "recruiter_signal",
                       "triaged_at")
SHOWN_KEYS = {"score", "rank", "band"}
SNAPSHOT_VERSION = 2
DISPATCH_OFF = ("the job_apply module is off (config.yaml modules.job_apply.enabled): Applied, prep and terminal "
                "jobs are disabled")
DECISION_VIEW_KEYS = ("decision", "job_status", "prep_file", "airtable_record_id", "band_override", "still_open")
PENDING_VIEW_KEYS = ("action", "reason", "note", "requested_at", "execute_after", "target_band")
PREP_TIMEOUT_S = 1200
APPLIED_TIMEOUT_S = 1800
TIMEOUT_SUFFIX = " — check Airtable / Prep before retrying"
# Prep runs in two steps (docs/ARCHITECTURE.md, "Form extractor"): a deterministic extractor
# subprocess reads the form into an inert JSON file, then a locked-down drafter (prep_lockdown.py: no browser, no
# web, no shell, writes only Prep/*.md) turns it into the pack. The extractor never fails the job: on any problem the
# drafter gets a stub (questions_visible: false) and writes the standard-set pack.
EXTRACT_TIMEOUT_S = 180         # dashboard hard stop for the extractor subprocess (process tree killed)
EXTRACT_INNER_TIMEOUT_S = 120   # the extractor's own --timeout
BACKEND_DIR = REPO_ROOT   # the extractor runs as `python -m prep_extract` from the repo root
FORM_FLAG = "--form"


def _rel_dir(p: Path) -> str:
    """A data folder as the repo-relative posix prefix the skills and the marker checks use ('data/prep/')."""
    try:
        return Path(p).resolve().relative_to(REPO_ROOT.resolve()).as_posix().rstrip("/") + "/"
    except ValueError:
        return Path(p).resolve().as_posix().rstrip("/") + "/"


def extractor_python() -> Path:
    return Path(config.module("form_extractor").get("python") or sys.executable)


FORMS_DIR = _rel_dir(config.forms_dir())
FORM_MAX_BYTES = 2_000_000
ACTIVE_STATES = ("running", "starting", "extracting")
EXTRACT_RESTART_ERROR = "dashboard restarted while reading the form — tap Retry to start again"
URL_RE = re.compile(r"https?://\S+")
FLAGS = {"applied": "--already-applied", "prep": "--prep"}
PREP_DIR = _rel_dir(config.prep_dir())
NOT_CONFIRMED_ERROR = "dispatch not confirmed — check Airtable / Prep before retrying"
RESTART_ERROR = "dashboard restarted during job — check Airtable / Prep before retrying"
# Marker lines (spec §4/§6). Line-anchored; tolerant of the backticks/bold a model adds; last match wins.
# A record id is an Airtable record (rec + 14) or, when the Airtable module is off, a local application record
# (a canonical lowercase UUID, uuid5 from scripts/record_id.py) that /job-apply writes to data/applications.jsonl.
# The local form is this extraction's only addition to the marker contract; the private system logs to Airtable only.
RECORD_ID = r"rec[A-Za-z0-9]{14}|[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}"
AIRTABLE_RE = re.compile(r"^[\s`*>_-]*AIRTABLE_RECORD:[\s`*]*(" + RECORD_ID + r")[\s`*_]*$", re.M)
PREP_RE = re.compile(r"^[\s`*>_-]*PREP_FILE:[\s`*]*([^`*\r\n]+?)[\s`*_]*$", re.M)
FROM_TERMINAL_FLAG = "--from-terminal"
# Terminal completion inbox (2026-10-03). `/job-apply <url> --lead <lead file>` (interactive, in the user's terminal)
# appends one line here after its Airtable write; list_leads() applies it. The skill never writes Leads/.
INBOX_REL = _rel_dir(config.data_dir()) + "terminal_completions.jsonl"
INBOX_STATE_NAME = "terminal_completions.processed.json"  # sidecar: what the dashboard did with each line
INBOX_SOURCE = "terminal-job-apply"
RECORD_ID_RE = re.compile(RECORD_ID)
INBOX_FINAL = ("applied", "noop", "rejected")  # a line with one of these outcomes is never looked at again


def still_open_active(lead: dict) -> bool:
    """Still open holds only for the triage the user answered; a newer triage (new triaged_at) voids it.
    Mirrors stillOpenActive in jobs_scoring.js."""
    so = (lead.get("decision") or {}).get("still_open")
    t = lead.get("triage") or {}
    return isinstance(so, dict) and (t.get("triaged_at") is None or so.get("triaged_at_seen") == t.get("triaged_at"))


def step_band(band, action) -> str | None:
    """One band per tap; mirrors jobs_scoring.js stepBand (unknown band or past an edge -> None)."""
    if band not in BAND_KEYS:
        return None
    i = BAND_KEYS.index(band) + (-1 if action == "move_up" else 1)
    return BAND_KEYS[i] if 0 <= i < len(BAND_KEYS) else None


def _on_board(lead: dict) -> bool:
    """Ranking-engine 4.11: only ranked-board cards move (v2, route board, not mid-help)."""
    t, d = lead.get("triage") or {}, lead.get("decision") or {}
    on = t.get("route") == "board" or (t.get("route") == "closed" and still_open_active(lead))
    return lead.get("schema_version", 1) >= 2 and on and d.get("decision") != "needs_help"


class JobsError(Exception):
    def __init__(self, status: int, detail: str):
        super().__init__(detail)
        self.status = status
        self.detail = detail


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _iso(dt: datetime) -> str:
    return dt.isoformat()


def _execute_after(pending) -> datetime | None:
    try:
        return datetime.fromisoformat(pending["execute_after"])
    except (KeyError, TypeError, ValueError):
        return None


def _config_snapshot(config_store) -> dict | None:
    if config_store is None:
        return None
    doc, _ = config_store.load()
    return {"version": doc["version"], "weights": doc["active_weights"],
            "floor": doc["compensation_settings"]["annual_base_floor"], "bands": doc["bands"]}


def snapshot(lead: dict, config: dict | None = None, shown: dict | None = None) -> dict:
    """What the user saw when they acted — stored with every decision for sub-project 2b (ranking-engine §5, D8).
    v1 keys stay (null on v2 leads: effective_bucket is None for v2); v2 adds the scoring inputs, the config in
    force and the client-reported score."""
    t = lead.get("triage") or {}
    d = lead.get("decision") or {}
    snap = {"snapshot_version": SNAPSHOT_VERSION, "bucket": LeadStore.effective_bucket(lead),
            "category": t.get("category"), "relevance": t.get("relevance"), "fit_score": t.get("fit_score"),
            "location": t.get("location"), "pay": t.get("pay"), "company": t.get("company"),
            "role_title": t.get("role_title"), "warm_contact": warm_current_contact(t.get("network")) is not None,
            "bucket_reason": t.get("bucket_reason"), "schema_version": lead.get("schema_version", 1)}
    snap.update({k: t.get(k) for k in SNAPSHOT_INPUT_KEYS})
    snap["first_seen"] = (lead.get("intake") or {}).get("created_at")   # same as view(): the freshness input
    snap["still_open"] = d.get("still_open")
    snap["config"] = config
    snap["shown_score"] = (shown or {}).get("score")
    snap["shown_rank"] = (shown or {}).get("rank")
    snap["shown_band"] = (shown or {}).get("band")
    return snap


def check_shown(shown) -> str | None:
    if shown is None:
        return None
    if not isinstance(shown, dict) or set(shown) != SHOWN_KEYS:
        return "shown must be {score, rank, band}"
    if shown["band"] not in BAND_KEYS:
        return f"shown.band: one of {', '.join(BAND_KEYS)}"
    s, r = shown["score"], shown["rank"]
    if not (isinstance(s, int) and not isinstance(s, bool) and 1 <= s <= 100):
        return "shown.score: integer 1-100"
    if not (isinstance(r, int) and not isinstance(r, bool) and 1 <= r <= 1000):
        return "shown.rank: integer 1-1000"
    return None


def _mark_history(history, requested_at, state, error=None) -> list:
    out = [dict(h) for h in (history or []) if isinstance(h, dict)]
    for h in reversed(out):
        if h.get("at") == requested_at and h.get("state") == "pending":
            h["state"] = state
            if error:
                h["error"] = error
            break
    return out


def skip_record(lead: dict, pending: dict, now: datetime) -> dict:
    """One `_skips.jsonl` line (spec §4) — the learning record that outlives the deleted lead."""
    t = lead.get("triage") or {}
    snap = pending.get("snapshot") or snapshot(lead)
    rec = {"lead_id": lead["lead_id"], "canonical_url": lead.get("canonical_url"), "company": t.get("company"),
           "role_title": t.get("role_title"), "category": t.get("category"), "relevance": t.get("relevance"),
           "bucket": snap.get("bucket"), "fit_score": t.get("fit_score"), "location": t.get("location"),
           "pay": t.get("pay"), "warm_contact": bool(snap.get("warm_contact")), "reason": pending.get("reason"),
           "note": pending.get("note"), "skipped_at": _iso(now)}
    rec.update({"snapshot_version": snap.get("snapshot_version", 1), "schema_version": snap.get("schema_version", 1),
                "scores": snap.get("scores"), "pay_bands": snap.get("pay_bands"),
                "posted_date": snap.get("posted_date"), "config": snap.get("config"),
                "shown_score": snap.get("shown_score"), "shown_rank": snap.get("shown_rank")})
    return rec


def parse_airtable_record(text) -> str | None:
    found = AIRTABLE_RE.findall(text or "")
    return found[-1] if found else None


def parse_prep_file(text) -> str | None:
    found = PREP_RE.findall(text or "")
    return found[-1].strip().replace("\\", "/") if found else None


def _last_line(text) -> str:
    lines = [l.strip() for l in str(text or "").splitlines() if l.strip()]
    return lines[-1][:200] if lines else "(no output)"


def _dispatch_create(request_text, repo_root, extra_allowed_tools, timeout_s, disallowed_tools=None,
                     settings_path=None, pack_saved=None):
    import dispatch  # lazy: tests inject fakes and never load the CLI layer
    kw = {} if timeout_s is None else {"timeout_s": timeout_s}
    if disallowed_tools:
        kw["disallowed_tools"] = disallowed_tools
    if settings_path:
        kw["settings_path"] = settings_path
    if pack_saved is not None:
        kw["pack_saved"] = pack_saved
    return dispatch.create_dispatch_job(request_text, repo_root, extra_allowed_tools=extra_allowed_tools, **kw)


def _dispatch_get(job_id):
    import dispatch
    return dispatch.get_dispatch_job(job_id)


def _dispatch_grant_once(request_text, tool):
    import dispatch
    return dispatch.accumulate_once_tool(request_text, tool)


def _run_extractor(url: str, out_path: Path, timeout_s: int, argv_prefix: list[str] | None = None) -> None:
    """Run the extractor as a plain subprocess (no shell, no claude -p). Raises RuntimeError on a missing venv,
    a non-zero exit or the hard timeout; on timeout the whole process tree (Chromium included) is killed."""
    if argv_prefix is None:
        if not config.enabled("form_extractor"):
            raise RuntimeError("the form_extractor module is off; the drafter writes the standard-set pack")
        py = extractor_python()
        if not py.is_file():
            raise RuntimeError(f"extractor interpreter missing ({py})")
        argv_prefix = [str(py), "-m", "prep_extract"]
    cmd = [*argv_prefix, "--url", url, "--out", str(out_path), "--timeout", str(EXTRACT_INNER_TIMEOUT_S)]
    proc = subprocess.Popen(cmd, cwd=str(BACKEND_DIR), stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                            creationflags=getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0),
                            start_new_session=os.name != "nt")
    try:
        _, err = proc.communicate(timeout=timeout_s)
    except subprocess.TimeoutExpired:
        if os.name == "nt":
            subprocess.run(["taskkill", "/T", "/F", "/PID", str(proc.pid)], capture_output=True)
        else:
            try:
                os.killpg(proc.pid, 9)
            except OSError:
                pass
        proc.kill()
        try:  # bounded: a descendant that kept the pipes open must not strand the lead in `extracting`
            proc.communicate(timeout=10)
        except subprocess.TimeoutExpired:
            pass
        raise RuntimeError(f"extractor timed out after {timeout_s} s")
    if proc.returncode != 0:
        raise RuntimeError(f"extractor exit {proc.returncode}: {_last_line(err.decode('utf-8', 'replace'))}")


def _spawn_thread(fn) -> None:
    threading.Thread(target=fn, daemon=True, name="prep-extract").start()


def _posting_url(lead: dict) -> str | None:
    """The lead's posting URL, only if it is a plain http(s) URL (never a javascript: or other scheme)."""
    t = lead.get("triage") or {}
    for u in (t.get("jd_url"), lead.get("canonical_url")):
        if isinstance(u, str) and URL_RE.fullmatch(u.strip()):
            return u.strip()
    return None


def _check_form_file(path: Path) -> None:
    if path.stat().st_size > FORM_MAX_BYTES:
        raise ValueError("extractor output is too large")
    data = json.loads(path.read_text(encoding="utf-8"))
    if not (isinstance(data, dict) and data.get("schema_version") == FORM_SCHEMA_VERSION
            and isinstance(data.get("questions_visible"), bool) and isinstance(data.get("pages"), list)):
        raise ValueError("extractor output is not a form_fields.json")


def check_inbox_line(line) -> str | None:
    """Shape check for one parsed inbox line. None = well-formed, else the rejection reason."""
    if not isinstance(line, dict):
        return "not a JSON object"
    if not (isinstance(line.get("lead_id"), str) and LEAD_ID_RE.fullmatch(line["lead_id"])):
        return f"lead_id is not a 12-hex lead id: {line.get('lead_id')!r}"
    rec = line.get("airtable_record_id")
    if not (isinstance(rec, str) and RECORD_ID_RE.fullmatch(rec)):
        return f"airtable_record_id is not rec + 14 characters or a lowercase UUID: {rec!r}"
    if line.get("source") != INBOX_SOURCE:
        return f"source is not {INBOX_SOURCE!r}: {line.get('source')!r}"
    return None


def _write_form_stub(path: Path, url, error: str) -> None:
    path.write_text(json.dumps(stub_result(url or "", error), ensure_ascii=False, indent=2), encoding="utf-8")


class JobsService:
    def __init__(self, repo_root: Path = REPO_ROOT, leads_dir: Path | None = None, clock=_utcnow,
                 create_job=_dispatch_create, get_job=_dispatch_get, grant_once=_dispatch_grant_once,
                 undo_window_s: int = UNDO_WINDOW_S, run_extractor=_run_extractor, spawn=_spawn_thread,
                 prepare_lockdown=prep_lockdown.prepare, inbox_path: Path | None = None, config_store=None,
                 dispatch_enabled: bool = True, demo: bool = False):
        self.config_store = config_store
        self.dispatch_enabled = dispatch_enabled  # server.py passes config modules.job_apply.enabled
        # Demo mode (python -m copilot demo): Applied and Prep run locally through board/demo.py, never dispatch.
        self.demo = demo
        self.repo_root = Path(repo_root)
        self.inbox_path = Path(inbox_path) if inbox_path else self.repo_root / INBOX_REL
        self.inbox_state_path = self.inbox_path.with_name(INBOX_STATE_NAME)
        self.store = LeadStore(Path(leads_dir) if leads_dir else config.leads_dir())
        self.clock = clock
        self._create_job = create_job
        self._get_job = get_job
        self._grant_once = grant_once
        self._run_extractor = run_extractor
        self._spawn = spawn
        self._prepare_lockdown = prepare_lockdown
        self._extracting_ids: dict[str, str] = {}  # lid -> form_file of the live reader; leads whose form reader is alive in THIS process
        self.undo_window_s = undo_window_s
        self.lock = threading.RLock()  # HTTP worker threads, the sweeper and reader threads share it

    # ---- reading ----------------------------------------------------------------
    def _read(self, lid: str) -> dict:
        if not LEAD_ID_RE.fullmatch(lid or ""):
            raise JobsError(404, f"no lead {lid!r}")
        try:
            lead = self.store.read(lid)
        except LeadStoreError as e:
            raise JobsError(404, str(e))
        if not isinstance(lead, dict):
            raise JobsError(404, f"lead {lid} is not a JSON object")
        return lead

    def _all(self) -> list[dict]:
        # Callers must hold self.lock: warnings.catch_warnings is not thread-safe.
        with warnings.catch_warnings(record=True) as caught:
            warnings.simplefilter("always")
            leads = self.store.list()
        for w in caught:
            print(f"[jobs] {w.message}", flush=True)
        return [l for l in leads if isinstance(l, dict) and LEAD_ID_RE.fullmatch(str(l.get("lead_id") or ""))]

    def view(self, lead: dict, now: datetime | None = None) -> dict:
        now = now or self.clock()
        t = lead.get("triage") or {}
        d = lead.get("decision") or {}
        p = d.get("pending")
        pending, remaining = None, None
        if isinstance(p, dict):
            pending = {k: p.get(k) for k in PENDING_VIEW_KEYS}
            ea = _execute_after(p)
            remaining = max(0, math.ceil((ea - now).total_seconds())) if ea else 0
        return {"lead_id": lead.get("lead_id"), "canonical_url": lead.get("canonical_url"),
                "schema_version": lead.get("schema_version", 1),
                "first_seen": (lead.get("intake") or {}).get("created_at"),
                "source_note": next((s.get("note") for s in reversed((lead.get("intake") or {}).get("sources") or [])
                                     if isinstance(s, dict) and s.get("note")), None),
                "effective_bucket": LeadStore.effective_bucket(lead),
                "triage": {k: t.get(k) for k in TRIAGE_VIEW_KEYS},
                "decision": {**{k: d.get(k) for k in DECISION_VIEW_KEYS}, "pending": pending},
                "undo_remaining_s": remaining}

    def list_leads(self) -> dict:
        with self.lock:  # _all() uses warnings.catch_warnings; the sweeper calls it under this lock too
            now = self.clock()
            try:
                self.process_inbox(now)
            except Exception as e:  # the inbox never breaks the list
                print(f"[jobs] terminal inbox skipped: {e!r}", flush=True)
            return {"server_now": _iso(now), "undo_window_s": self.undo_window_s,
                    "leads_dir": self._leads_ref(), "dispatch_enabled": self.dispatch_enabled, "demo": self.demo,
                    "leads": [self.view(l, now) for l in self._all()]}

    def _leads_ref(self) -> str:
        """The leads folder as the terminal command should name it (repo-relative when it can be)."""
        try:
            return self.store.base.resolve().relative_to(self.repo_root.resolve()).as_posix() + "/"
        except ValueError:
            return self.store.base.resolve().as_posix() + "/"

    # ---- terminal completion inbox -------------------------------------------------
    # The inbox is append-only and never rewritten by the dashboard. Each complete line is keyed by the sha256 of
    # its text in a sidecar (INBOX_STATE_NAME) with its outcome: applied / noop / rejected are final and skipped
    # from then on; deferred (lead not yet terminal-in-progress, pending or busy) is re-checked on every load. The
    # sidecar is a log and a fast path, not the safety mechanism: applying a line is idempotent (an applied lead is
    # a no-op), so a lost or corrupt sidecar only means every line is re-checked once.
    def _load_inbox_state(self) -> dict:
        try:
            data = json.loads(self.inbox_state_path.read_text(encoding="utf-8"))
            if isinstance(data, dict) and isinstance(data.get("lines"), dict):
                return data
            print(f"[jobs] {INBOX_STATE_NAME} has no lines map; re-checking every line", flush=True)
        except FileNotFoundError:
            pass
        except (OSError, ValueError) as e:
            print(f"[jobs] {INBOX_STATE_NAME} unreadable ({e!r}); re-checking every line", flush=True)
        return {"version": 1, "lines": {}}

    def _save_inbox_state(self, state: dict) -> None:
        tmp = self.inbox_state_path.with_name(INBOX_STATE_NAME + ".tmp")
        tmp.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")
        os.replace(tmp, self.inbox_state_path)

    def process_inbox(self, now: datetime | None = None) -> None:
        """Apply new terminal-completion lines. Caller holds self.lock. A bad line never raises."""
        now = now or self.clock()
        try:
            text = self.inbox_path.read_text(encoding="utf-8")
        except FileNotFoundError:
            return
        text = text[: text.rfind("\n") + 1]  # a writer may be mid-append: the unterminated tail waits for next load
        state = self._load_inbox_state()
        seen, changed = state["lines"], False
        for n, raw in enumerate(text.splitlines(), 1):
            line = raw.strip()
            if not line:
                continue
            key = hashlib.sha256(line.encode("utf-8")).hexdigest()
            prev = seen.get(key) or {}
            if prev.get("outcome") in INBOX_FINAL:
                continue
            try:
                outcome, reason, lid = self._apply_inbox_line(line, now)
            except Exception as e:  # one bad line never stops the rest; re-checked next load
                outcome, reason, lid = "deferred", f"error: {e!r}", None
            if (outcome, reason) != (prev.get("outcome"), prev.get("reason")):  # log each change once, not per poll
                print(f"[jobs] terminal inbox line {n}: {outcome} ({reason})", flush=True)
                seen[key] = {"outcome": outcome, "reason": reason, "lead_id": lid, "line": n, "at": _iso(now)}
                changed = True
        if changed:
            self._save_inbox_state(state)

    def _apply_inbox_line(self, text: str, now: datetime) -> tuple[str, str, str | None]:
        """-> (outcome, reason, lead_id). Outcomes: applied | noop | rejected (final) | deferred (re-checked)."""
        try:
            line = json.loads(text)
        except ValueError as e:
            return "rejected", f"not JSON: {e}", None
        problem = check_inbox_line(line)
        if problem:
            lid = line.get("lead_id") if isinstance(line, dict) and isinstance(line.get("lead_id"), str) else None
            return "rejected", problem, lid
        lid, rec = line["lead_id"], line["airtable_record_id"]
        try:
            lead = self.store.read(lid)
        except LeadStoreError as e:
            return "rejected", f"unknown lead: {e}", lid
        if not isinstance(lead, dict):
            return "rejected", "lead file is not a JSON object", lid
        dec = lead.get("decision") or {}
        js = dec.get("job_status") or {}
        if dec.get("decision") == "applied":
            other = dec.get("airtable_record_id")
            return "noop", "already applied" + (f" with {other}" if other and other != rec else ""), lid
        if js.get("state") in ACTIVE_STATES:
            return "deferred", f"a {js.get('kind')} job is {js.get('state')}", lid
        if dec.get("pending"):
            return "deferred", f"a {(dec.get('pending') or {}).get('action')} action is pending", lid
        if js.get("kind") != "terminal" or js.get("state") != "in_progress":
            return "deferred", f"lead is not in terminal progress (job {js.get('kind')}/{js.get('state')})", lid
        at = _iso(now)
        history = list(dec.get("history") or []) + [
            {"at": at, "action": "applied", "via": "terminal", "reason": None, "note": None, "state": "done",
             "record_id": rec, "completed_at": line.get("completed_at"), "snapshot": snapshot(lead)}]
        self.store.update(lid, "decision", {
            "decision": "applied", "airtable_record_id": rec, "history": history,
            "job_status": {"kind": "applied", "state": "done", "record_id": rec, "updated_at": at}})
        return "applied", f"applied {rec}", lid

    # ---- actions ----------------------------------------------------------------
    def record_action(self, lid: str, action: str, reason: str | None = None, note: str | None = None,
                      shown: dict | None = None) -> dict:
        if action not in ACTIONS:
            raise JobsError(422, f"unknown action {action!r}; expected one of {', '.join(ACTIONS)}")
        if action in JOB_ACTIONS and not (self.dispatch_enabled or self.demo):
            raise JobsError(503, DISPATCH_OFF)
        if action == "skip" and reason not in SKIP_REASONS:
            raise JobsError(422, f"skip needs a reason chip: {', '.join(SKIP_REASONS)}")
        if note is not None and len(note) > NOTE_MAX:
            raise JobsError(422, f"note longer than {NOTE_MAX} characters")
        err = check_shown(shown)
        if err:
            raise JobsError(422, err)
        if action in BAND_ACTIONS and shown is None:
            raise JobsError(422, "a band move needs shown {score, rank, band}")
        with self.lock:
            lead = self._read(lid)
            dec = lead.get("decision") or {}
            if dec.get("pending"):
                raise JobsError(409, "an action is already pending for this lead")
            if action in JOB_ACTIONS and (dec.get("job_status") or {}).get("state") in ACTIVE_STATES:
                raise JobsError(409, "a background job is still running for this lead")
            if dec.get("decision") == "applied":
                raise JobsError(409, "already marked applied; use Retry if logging failed")
            now = self.clock()
            at = _iso(now)
            if action == "still_open" and ((lead.get("triage") or {}).get("route") != "closed" or still_open_active(lead)):
                raise JobsError(409, "only a Closed? card can be marked still open")
            extra, target = {}, None
            if action in BAND_ACTIONS:
                if not _on_board(lead):
                    raise JobsError(409, "only cards on the ranked board can move between bands")
                ov = (dec.get("band_override") or {}).get("band")
                computed = shown["band"]
                if action == "reset_band":
                    if not ov:
                        raise JobsError(409, "this card has not been moved")
                else:
                    target = step_band(ov or computed, action)
                    if target is None:
                        raise JobsError(409, "already in the top band" if action == "move_up" else "already in the bottom band")
                extra = {"computed_band": computed, "chosen_band": target}
            snap = {**snapshot(lead, _config_snapshot(self.config_store), shown), **extra}
            pending = {"action": action, "reason": reason, "note": note, "requested_at": at, "target_band": target,
                       "execute_after": _iso(now + timedelta(seconds=self.undo_window_s)), "snapshot": snap}
            if action == "still_open":
                pending["triaged_at_seen"] = (lead.get("triage") or {}).get("triaged_at")
            history = list(dec.get("history") or []) + [
                {"at": at, "action": action, "reason": reason, "note": note, "state": "pending", "snapshot": snap}]
            return self.view(self.store.update(lid, "decision", {"pending": pending, "history": history}), now)

    def undo(self, lid: str) -> dict:
        with self.lock:
            lead = self._read(lid)
            dec = lead.get("decision") or {}
            p = dec.get("pending")
            if not isinstance(p, dict):
                raise JobsError(409, "nothing pending to undo")
            now = self.clock()
            ea = _execute_after(p)
            if ea is not None and now >= ea:  # a corrupt execute_after never executes, so it stays undoable
                raise JobsError(409, "the undo window has closed")
            history = _mark_history(dec.get("history"), p.get("requested_at"), "undone")
            return self.view(self.store.update(lid, "decision", {"pending": None, "history": history}), now)


    # ---- sweeper ----------------------------------------------------------------
    def sweep(self) -> None:
        """Execute every due pending action. Called every SWEEP_INTERVAL_S and once at startup."""
        with self.lock:
            now = self.clock()
            for lead in self._all():
                try:
                    self._sweep_one(lead, now)
                except Exception as e:  # one bad lead never stops the rest
                    print(f"[jobs] sweep failed for {lead.get('lead_id')}: {e!r}", flush=True)

    def _sweep_one(self, lead: dict, now: datetime) -> None:
        dec = lead.get("decision") or {}
        p = dec.get("pending")
        if isinstance(p, dict):
            ea = _execute_after(p)
            if ea is None:
                print(f"[jobs] {lead['lead_id']}: pending action has no valid execute_after; left for Undo", flush=True)
                return
            if ea <= now:
                self._execute(lead, p, now)
            return
        state = (dec.get("job_status") or {}).get("state")
        if state == "extracting":
            if lead["lead_id"] not in self._extracting_ids:  # its reader died with an earlier process
                self._fail_job(lead, EXTRACT_RESTART_ERROR, now)  # never re-run automatically
            return
        if state in ("running", "starting"):
            self._poll_job(lead, now)

    def _execute(self, lead: dict, pending: dict, now: datetime) -> None:
        handler = getattr(self, f"_exec_{pending.get('action')}", None)
        if handler is None:
            print(f"[jobs] {lead['lead_id']}: no executor for {pending.get('action')!r}; left pending", flush=True)
            return
        handler(lead, pending, now)

    def _finish(self, lead: dict, pending: dict, fields: dict, state: str = "done", error: str | None = None):
        dec = lead.get("decision") or {}
        history = _mark_history(dec.get("history"), pending.get("requested_at"), state, error)
        self.store.update(lead["lead_id"], "decision", {**fields, "pending": None, "history": history})

    def _set_band(self, lead, pending, now):
        snap = pending.get("snapshot") or {}
        chosen, computed = snap.get("chosen_band"), snap.get("computed_band")
        ov = None if chosen in (None, computed) else {"band": chosen, "computed_band": computed,
                                                       "score": snap.get("shown_score"), "set_at": _iso(now)}
        self._finish(lead, pending, {"band_override": ov})

    def _exec_still_open(self, lead, pending, now):
        # the triaged_at the user saw at tap time (captured in record_action), not whatever the file holds now
        self._finish(lead, pending, {"still_open": {"at": _iso(now), "by": "user",
                                                    "triaged_at_seen": pending.get("triaged_at_seen")}})

    def _exec_move_up(self, lead, pending, now):
        self._set_band(lead, pending, now)

    def _exec_move_down(self, lead, pending, now):
        self._set_band(lead, pending, now)

    def _exec_reset_band(self, lead, pending, now):
        self._finish(lead, pending, {"band_override": None})

    def _exec_terminal(self, lead, pending, now):
        self._finish(lead, pending, {"decision": "needs_help",
                                     "job_status": {"kind": "terminal", "state": "in_progress",
                                                    "updated_at": _iso(now)}})

    def _exec_skip(self, lead, pending, now):
        lid = lead["lead_id"]
        if lid not in self.store.skipped_ids():  # idempotent: a failed delete is retried next sweep
            self.store.append_skip(skip_record(lead, pending, now))
        self.store.delete(lid)


    # ---- dispatched jobs --------------------------------------------------------
    def request_text(self, kind: str, lid: str, form_file: str | None = None, from_terminal: bool = False) -> str:
        path = (self.store.base / f"{lid}.json").resolve()
        try:
            ref = path.relative_to(self.repo_root.resolve()).as_posix()
        except ValueError:
            ref = path.as_posix()
        text = f"/job-apply {FLAGS[kind]} {ref}"
        if from_terminal and kind == "applied":
            text = f"{text} {FROM_TERMINAL_FLAG}"
        return f"{text} {FORM_FLAG} {form_file}" if form_file else text

    def _starting(self, kind: str, lid: str, now: datetime, from_terminal: bool = False) -> dict:
        js = {"kind": kind, "request": self.request_text(kind, lid, from_terminal=from_terminal),
              "state": "starting", "requested_at": _iso(now), "updated_at": _iso(now)}
        if from_terminal:
            js["from_terminal"] = True  # kept on the job so Retry re-dispatches the same request
        return js

    def _launch(self, lid: str, js: dict, now: datetime, extra_allowed_tools=None, settings_path=None) -> dict:
        """Dispatch a job whose `starting` state is ALREADY on disk (write-first: if a later write fails, the
        sweeper finds `starting` without a job_id and fails it; it never re-dispatches). Returns the stored lead."""
        kind = js["kind"]
        timeout_s = PREP_TIMEOUT_S if kind == "prep" else APPLIED_TIMEOUT_S
        try:
            if kind == "prep":  # the lockdown settings file was prepared (and self-tested) before the lock was taken
                job_id = self._create_job(js["request"], self.repo_root, None, timeout_s,
                                          list(prep_lockdown.DRAFTER_DENY), settings_path=str(settings_path),
                                          pack_saved=self._pack_saved)
            else:
                job_id = self._create_job(js["request"], self.repo_root, extra_allowed_tools, timeout_s)
        except Exception as e:
            lead = self.store.update(lid, "decision", {"job_status": {
                **js, "state": "failed", "error": f"could not start job: {e}", "updated_at": _iso(now)}})
            self._fail_history(lead, f"could not start job: {e}")
            return self.store.read(lid)
        return self.store.update(lid, "decision", {"job_status": {
            **js, "state": "running", "job_id": job_id, "started_at": _iso(now), "updated_at": _iso(now)}})

    def _fail_history(self, lead: dict, error: str) -> None:
        """If the last history row came from the action that just dispatched, mark it failed (it was written done)."""
        hist = [dict(h) for h in ((lead.get("decision") or {}).get("history") or []) if isinstance(h, dict)]
        if hist and hist[-1].get("state") == "done" and hist[-1].get("action") in FLAGS:
            hist[-1].update(state="failed", error=error)
            self.store.update(lead["lead_id"], "decision", {"history": hist})

    def _fail_job(self, lead: dict, error: str, now: datetime) -> None:
        js = dict((lead.get("decision") or {}).get("job_status") or {})
        js.update(state="failed", error=error, updated_at=_iso(now))
        self._fail_history(self.store.update(lead["lead_id"], "decision", {"job_status": js}), error)

    def _exec_dispatch(self, lead, pending, now, kind, decision, from_terminal=False):
        lid = lead["lead_id"]
        js = self._starting(kind, lid, now, from_terminal)
        self._finish(lead, pending, {"decision": decision, "job_status": js})  # clears pending BEFORE dispatching
        self._launch(lid, js, now)

    def _exec_applied(self, lead, pending, now):
        # "Applied (done in terminal)" fallback: the terminal session already did the work, so the job only finds
        # and reuses that Airtable record (SKILL.md --from-terminal).
        js = (lead.get("decision") or {}).get("job_status") or {}
        from_terminal = js.get("kind") == "terminal" and js.get("state") == "in_progress"
        if self.demo:
            return self._demo_applied(lead, pending, now)
        self._exec_dispatch(lead, pending, now, "applied", "applied", from_terminal)

    def _demo_applied(self, lead, pending, now):
        import demo
        rec = demo.write_local_record(self.store.base.parent, lead, now)
        self._finish(lead, pending, {"decision": "applied", "airtable_record_id": rec, "job_status": {
            "kind": "applied", "state": "done", "record_id": rec, "demo": True, "updated_at": _iso(now)}})

    def _demo_prep(self, lead, pending, now):
        import demo
        path = demo.write_prep_pack(self.store.base.parent / "prep", lead, now)
        try:
            ref = path.resolve().relative_to(self.repo_root.resolve()).as_posix()
        except ValueError:
            ref = path.resolve().as_posix()
        self._finish(lead, pending, {"decision": "needs_help", "prep_file": ref, "job_status": {
            "kind": "prep", "state": "done", "prep_file": ref, "demo": True, "updated_at": _iso(now)}})

    def _exec_prep(self, lead, pending, now):
        if self.demo:
            return self._demo_prep(lead, pending, now)
        lid = lead["lead_id"]
        js = self._extracting(lid, now)
        self._finish(lead, pending, {"decision": "needs_help", "job_status": js})  # `extracting` is on disk first
        self._begin_extract(lid, js)

    def _extracting(self, lid: str, now: datetime) -> dict:
        stamp = now.astimezone(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        form_file = f"{FORMS_DIR}{lid}-{stamp}.json"
        return {"kind": "prep", "request": self.request_text("prep", lid, form_file), "form_file": form_file,
                "state": "extracting", "requested_at": _iso(now), "updated_at": _iso(now)}

    def _begin_extract(self, lid: str, js: dict) -> None:
        self._extracting_ids[lid] = js["form_file"]
        try:
            self._spawn(lambda: self._extract_then_launch(lid, js))
        except Exception as e:  # could not even start the reader thread
            self._forget_reader(lid, js)
            self._fail_extract(lid, js, f"could not start the form reader: {e}")

    def _extract_then_launch(self, lid: str, js: dict) -> None:
        """Reader thread: run the extractor WITHOUT the lock (up to 180 s), then, under the lock, write `starting`
        and dispatch — only if this request is still the lead's current `extracting` state."""
        try:
            with self.lock:
                url = _posting_url(self.store.read(lid))
            out = self.repo_root / js["form_file"]
            problem = None
            try:
                out.parent.mkdir(parents=True, exist_ok=True)
                if not url:
                    raise RuntimeError("the lead has no http(s) posting URL")
                self._run_extractor(url, out, EXTRACT_TIMEOUT_S)
            except Exception as e:
                problem = e
            if url:  # whatever the exit code, a valid file on disk wins (the CLI watchdog exits 1 after writing a stub)
                try:
                    _check_form_file(out)
                    problem = None
                except Exception as e:
                    problem = problem or e
            if problem is not None:
                _write_form_stub(out, url, f"extractor failed: {problem}")
            if not self._still_extracting(lid, js):
                return  # superseded: do not even create a lockdown folder
            # prepare() runs hook self-test subprocesses: never while holding self.lock. Fail closed: if it raises,
            # the job fails with a clear error and nothing is dispatched.
            try:
                settings_path = self._prepare_lockdown(self.repo_root, lid, self.clock())
            except Exception as e:
                self._fail_extract(lid, js, f"could not start job: {e}")
                return
            with self.lock:
                cur = (self.store.read(lid).get("decision") or {}).get("job_status") or {}
                if cur.get("state") != "extracting" or cur.get("form_file") != js["form_file"]:
                    return  # superseded (e.g. failed by a restart sweep): never launch over it
                now = self.clock()
                starting = {**js, "state": "starting", "updated_at": _iso(now)}
                self.store.update(lid, "decision", {"job_status": starting})  # write first, then dispatch
                self._launch(lid, starting, now, settings_path=settings_path)
        except Exception as e:
            print(f"[jobs] prep for {lid} failed before dispatch: {e!r}", flush=True)
            self._fail_extract(lid, js, f"could not prepare the prep job: {e}")
        finally:
            self._forget_reader(lid, js)

    def _forget_reader(self, lid: str, js: dict) -> None:
        """Drop this reader's entry only: a newer reader (fast Retry) may own the lead's slot now."""
        if self._extracting_ids.get(lid) == js.get("form_file"):
            self._extracting_ids.pop(lid, None)

    def _still_extracting(self, lid: str, js: dict) -> bool:
        with self.lock:
            cur = (self.store.read(lid).get("decision") or {}).get("job_status") or {}
        return cur.get("state") == "extracting" and cur.get("form_file") == js["form_file"]

    def _fail_extract(self, lid: str, js: dict, error: str) -> None:
        """Only an `extracting` state for this same request is failed; a `starting` one is left for the sweep
        (the drafter may already be running, so it must never become retryable here)."""
        try:
            with self.lock:
                lead = self.store.read(lid)
                cur = (lead.get("decision") or {}).get("job_status") or {}
                if cur.get("state") == "extracting" and cur.get("form_file") == js.get("form_file"):
                    self._fail_job(lead, error, self.clock())
        except Exception as e:
            print(f"[jobs] {lid}: could not record the prep failure: {e!r}", flush=True)

    def _check_prep_path(self, path: str | None) -> str | None:
        if not path:
            return None  # caller reports the missing marker
        parts = path.split("/")
        if not path.startswith(PREP_DIR) or ".." in parts or not path.endswith(".md") or path.endswith(".draft.md"):
            return f"PREP_FILE is not a prep pack under {PREP_DIR}: {path}"
        target = (self.repo_root / path).resolve()
        if not target.is_relative_to((self.repo_root / PREP_DIR).resolve()):
            return f"PREP_FILE resolves outside {PREP_DIR}: {path}"
        if not target.is_file():
            return f"PREP_FILE does not exist: {path}"
        return None

    def _pack_saved(self, result) -> bool:
        """Handed to dispatch for prep jobs, so the dispatch job list (jobs.json, dispatch-log.md) applies the card's
        rule: a valid PREP_FILE line naming a pack on disk means done, whatever tools the lockdown refused."""
        path, problem = self._prep_outcome(result)
        return bool(path) and problem is None

    def _prep_outcome(self, result) -> tuple[str | None, str | None]:
        path = parse_prep_file(result)
        problem = self._check_prep_path(path) if path else f"no PREP_FILE line; job said: {_last_line(result)}"
        return path, problem

    def _poll_job(self, lead: dict, now: datetime) -> None:
        dec = lead.get("decision") or {}
        js = dict(dec.get("job_status") or {})
        job = self._get_job(js["job_id"]) if js.get("job_id") else None
        fields = {}
        if js.get("state") == "starting" and not js.get("job_id"):  # never re-dispatch: the job may have started
            js.update(state="failed", error=NOT_CONFIRMED_ERROR)
        elif job is None:  # dispatch keeps jobs in memory only: unknown id = the dashboard restarted
            js.update(state="failed", error=RESTART_ERROR)
        else:
            status = job.get("status")
            if status in ("pending", "queued"):   # queued = waiting for one of dispatch's 3 slots
                return
            result = job.get("result") or ""
            if status == "complete" and js.get("kind") == "applied":
                rec = parse_airtable_record(result)
                if rec:
                    js.update(state="done", record_id=rec, error=None)
                    fields["airtable_record_id"] = rec
                else:
                    js.update(state="failed", error=f"no AIRTABLE_RECORD line; job said: {_last_line(result)}")
            elif status == "complete":
                path, problem = self._prep_outcome(result)
                if problem:
                    js.update(state="failed", error=problem)
                else:
                    js.update(state="done", prep_file=path, error=None)
                    if js.get("kind") == "prep" and job.get("blockedTools"):
                        js["blocked_tools"] = list(job["blockedTools"])  # dispatch already applied the same rule
                    fields["prep_file"] = path
            elif status == "permission_denied" and js.get("kind") == "prep":
                # The lockdown hook's denials land in permission_denials. Nothing can be granted to the drafter,
                # so a saved pack is a success (blocked tools kept for audit) and no pack is a plain failure.
                tools = list(job.get("deniedTools") or [])
                path, problem = self._prep_outcome(result)
                if problem:
                    js.update(state="failed", error=f"the locked-down drafter was blocked from "
                                                    f"{', '.join(tools) or 'a tool'} and saved no pack ({problem})")
                else:
                    js.update(state="done", prep_file=path, blocked_tools=tools, error=None)
                    fields["prep_file"] = path
            elif status == "permission_denied":
                tools = list(job.get("deniedTools") or [])
                if job.get("sensitiveBlock") or job.get("stuck"):
                    js.update(state="failed", error=f"permission for {', '.join(tools) or 'a tool'} cannot be "
                                                    "granted from the dashboard; continue in terminal")
                else:
                    js.update(state="needs_permission", denied_tools=tools, error=None)
            else:
                err = str(result).strip()[:300] or f"job {status}"
                if "Timed out" in err:
                    err += TIMEOUT_SUFFIX
                js.update(state="failed", error=err)
        js["updated_at"] = _iso(now)
        self.store.update(lead["lead_id"], "decision", {**fields, "job_status": js})

    def retry(self, lid: str, grant: bool = False) -> dict:
        if not self.dispatch_enabled:
            raise JobsError(503, DISPATCH_OFF)
        with self.lock:
            lead = self._read(lid)
            dec = lead.get("decision") or {}
            js = dec.get("job_status") or {}
            if js.get("kind") not in FLAGS or js.get("state") not in ("failed", "needs_permission"):
                raise JobsError(409, "nothing to retry: no failed or permission-blocked job")
            if dec.get("pending"):
                raise JobsError(409, "an action is pending for this lead")
            now = self.clock()
            if js["kind"] == "prep":
                if grant:
                    raise JobsError(409, "the prep drafter is locked down and never gets extra tools; "
                                         "tap Retry, or continue in terminal")
                ext = self._extracting(lid, now)
                self.store.update(lid, "decision", {"job_status": ext})  # write first, then read the form again
                self._begin_extract(lid, ext)
                return self.view(self.store.read(lid), now)
            extra = None
            if grant:
                if js.get("state") != "needs_permission" or not js.get("denied_tools"):
                    raise JobsError(409, "no denied tools to grant")
                req = self.request_text(js["kind"], lid, from_terminal=bool(js.get("from_terminal")))
                for tool in list(js["denied_tools"]):
                    extra = self._grant_once(req, tool)  # same once-grant accumulator /api/permissions/grant uses
            starting = self._starting(js["kind"], lid, now, bool(js.get("from_terminal")))
            self.store.update(lid, "decision", {"job_status": starting})  # write first, then dispatch
            return self.view(self._launch(lid, starting, now, extra), now)


def start_sweeper(svc: JobsService, interval_s: float = SWEEP_INTERVAL_S) -> threading.Event:
    """Daemon thread: one sweep right away (startup recovery), then every interval_s. Set the event to stop."""
    stop = threading.Event()

    def loop():
        while True:
            try:
                svc.sweep()
            except Exception as e:
                print(f"[jobs] sweeper error: {e!r}", flush=True)
            if stop.wait(interval_s):
                return

    threading.Thread(target=loop, daemon=True, name="jobs-sweeper").start()
    return stop


class ActionBody(BaseModel):
    action: str
    reason: str | None = None
    note: str | None = None
    shown: dict | None = None   # {score, rank, band} the card showed when the user acted (ranking-engine §5)


def _http(fn, *args):
    try:
        return fn(*args)
    except JobsError as e:
        raise HTTPException(status_code=e.status, detail=e.detail)


class RetryBody(BaseModel):
    grant: bool = False


def make_router(svc: JobsService) -> APIRouter:
    router = APIRouter()

    @router.get("/api/jobs/leads")
    def jobs_list_leads():
        return svc.list_leads()

    @router.post("/api/jobs/leads/{lead_id}/action")
    def jobs_action(lead_id: str, body: ActionBody):
        return _http(svc.record_action, lead_id, body.action, body.reason, body.note, body.shown)

    @router.post("/api/jobs/leads/{lead_id}/undo")
    def jobs_undo(lead_id: str):
        return _http(svc.undo, lead_id)

    @router.post("/api/jobs/leads/{lead_id}/retry")
    def jobs_retry(lead_id: str, body: RetryBody | None = None):
        return _http(svc.retry, lead_id, bool(body and body.grant))

    return router
