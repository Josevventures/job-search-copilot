"""Scheduled Slack intake (design: docs/ARCHITECTURE.md, "Slack intake"; decisions: docs/DESIGN-DECISIONS.md).

Reads the user's drops in one Slack channel, ingests them, runs /job-apply-fleet stages 2-4 headless (cap 20), and
replies in each message's thread. Slack is an inbox this script reads; the leads dir is the ledger it writes. Never
writes Airtable, never pushes git. Reads Workday JDs itself (lib/ats_fetch.py). Needs modules.slack_intake and
modules.ai_triage enabled in config.yaml. Usage (from the repo root):
    python skills/job-apply-fleet/jobs_slack_intake.py [--no-post] [--limit 20]
"""
import argparse
import functools
import importlib.util
import json
import os
import subprocess
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import leads_intake  # noqa: E402
from lib import ats_fetch, paths, slack_intake as si, slack_replies as sr  # noqa: E402
from lib.lead_store import LeadStore, LeadStoreError  # noqa: E402
from lib.prefs import load as load_prefs, screen_labels  # noqa: E402

from copilot import config  # noqa: E402  (lib.paths put the repo root on sys.path)

ENV_FILE = paths.REPO_ROOT / ".env"
BRIDGE = paths.REPO_ROOT / "board" / "scoring_bridge.py"
SCORING_CONFIG = paths.REPO_ROOT / "board" / "jobs_scoring_config.py"
MAX_LEADS = 20
HEADLESS_TIMEOUT_S = 90 * 60
STALE_LOCK_S = 3 * 3600
DEFER_TOTAL_S = 2.5 * 3600   # rulings C9/C9b: run time + quiet-hours hold must stay under the 3 h task limit
VENV_PY = Path(sys.executable).as_posix()   # the literal interpreter the headless run is allowed to call
SK = "skills/job-apply-fleet"

PROMPT = ("Run /job-apply-fleet in Unattended mode (SKILL.md, section 'Unattended mode'), stages 2-4 only. "
          "{merge}Batch id: {batch} - use this literal id in every command, never a shell variable or command substitution, and "
          "call Python as " + VENV_PY + ". "
          "Lead ids file: {ids}. Pass --batch {batch} --ids-file {ids} --limit {limit} to fleet_wave.py init. "
          "Write the result with fleet_wave.py result --batch {batch} --out {result}. Do not run stage 1. "
          "Never ask questions.")
MERGE = ("First, before anything else, merge the checkpoints of the interrupted previous run(s), in this order: {cmds}. "
         "Only then start the new batch below. ")
ALLOWED = ["Agent", "Read", "Write", "Edit", "Glob", "Grep", "WebFetch", "WebSearch", "Skill",
           f"Bash({VENV_PY}:*)", "Bash(python:*)", "Bash(date:*)",
           # Optional: a browser MCP for login-walled posts (subagent fetch ladder rung 4). Unused if not installed.
           "mcp__claude-in-chrome__tabs_context_mcp", "mcp__claude-in-chrome__tabs_create_mcp",
           "mcp__claude-in-chrome__navigate", "mcp__claude-in-chrome__get_page_text",
           "mcp__claude-in-chrome__read_page", "mcp__claude-in-chrome__tabs_close_mcp"]
AIRTABLE_READ = "mcp__claude_ai_Airtable__list_records_for_table"   # added only when modules.airtable is on
# Write-capable tools an interactive session may allow. A bare server name blocks every tool of that server.
DISALLOWED = ["mcp__claude_ai_Gmail", "mcp__claude_ai_Google_Calendar",
              "mcp__claude_ai_Gmail__send_message", "mcp__claude_ai_Gmail__create_draft",
              "mcp__claude_ai_Airtable__create_records_for_table", "mcp__claude_ai_Airtable__update_records_for_table",
              "mcp__claude_ai_Airtable__delete_records_for_table", "mcp__claude_ai_Airtable__create_field",
              "mcp__claude_ai_Airtable__update_field", "Bash(git push:*)", "Bash(git commit:*)"]
ERR_MAX = 200   # global constraint: error strings <= 200 chars


def log(line: str) -> None:
    d = paths.slack_intake_dir()
    d.mkdir(parents=True, exist_ok=True)
    with open(d / "runs.log", "a", encoding="utf-8", newline="\n") as f:
        f.write(f"{datetime.now(timezone.utc).isoformat(timespec='seconds')} {line}\n")


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def batch_id_for(run_dir) -> str:
    """The wrapper names the fleet batch after the run dir (ruling C2), so a failed run's batch is known."""
    return f"slack-{Path(run_dir).name}"


# ---- adapters -------------------------------------------------------------------------------

def _token():
    tok = os.environ.get("SLACK_BOT_TOKEN")
    if tok:
        return tok
    try:
        for ln in ENV_FILE.read_text(encoding="utf-8").splitlines():
            if ln.strip().startswith("SLACK_BOT_TOKEN="):
                return ln.split("=", 1)[1].strip().strip('"').strip("'")
    except OSError:
        pass
    return None


class Slack:
    def __init__(self, channel, token=None):
        from lib import slack_api  # noqa: PLC0415
        self.api, self.channel, self.token = slack_api, channel, token

    def history(self, oldest):
        return self.api.history_all(self.channel, oldest, token=self.token)

    def replies(self, ts):
        return self.api.replies(self.channel, ts, token=self.token)

    def post(self, text, thread_ts=None):
        return self.api.post(self.channel, text, thread_ts=thread_ts, token=self.token)


def run_headless(ids_path, result_path, limit, timeout_s=HEADLESS_TIMEOUT_S, run=subprocess.run,
                 merge_first=()) -> dict:
    merge = ""
    if merge_first:
        merge = MERGE.format(cmds="; ".join(f"{VENV_PY} {SK}/fleet_merge.py --batch {b}" for b in merge_first))
    prompt = PROMPT.format(merge=merge, batch=batch_id_for(Path(result_path).parent), ids=Path(ids_path).as_posix(),
                           limit=limit, result=Path(result_path).as_posix())
    allowed = ALLOWED + ([AIRTABLE_READ] if config.enabled("airtable") else [])
    model = config.module("ai_triage").get("model") or "sonnet"
    cmd = ["claude", "-p", "--model", model, "--output-format", "json", prompt,
           "--allowed-tools", ",".join(allowed), "--disallowed-tools", ",".join(DISALLOWED)]
    env = os.environ.copy()
    # With a claude.ai login, ANTHROPIC_API_KEY switches the CLI to API-key auth and disables the claude.ai
    # connectors (Airtable). Set JSC_KEEP_API_KEY=1 if you authenticate the CLI with an API key on purpose.
    if os.environ.get("JSC_KEEP_API_KEY") != "1":
        env.pop("ANTHROPIC_API_KEY", None)
    try:
        p = run(cmd, cwd=str(paths.REPO_ROOT), capture_output=True, timeout=timeout_s, env=env,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    except subprocess.TimeoutExpired:
        return {"ok": False, "error": f"headless run timed out after {timeout_s // 60} min"}
    Path(result_path).parent.joinpath("claude.json").write_bytes(p.stdout or b"")
    try:
        res = json.loads(Path(result_path).read_text(encoding="utf-8"))
    except FileNotFoundError:
        return {"ok": False, "error": f"no result.json (claude exit {p.returncode})"}
    except ValueError as e:
        return {"ok": False, "error": f"result.json unreadable: {e}"}
    return {**res, "ok": not res.get("stopped")}


def _bridge():
    spec = importlib.util.spec_from_file_location("scoring_bridge", BRIDGE)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _scoring_defaults() -> dict:
    spec = importlib.util.spec_from_file_location("jobs_scoring_config", SCORING_CONFIG)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod.defaults()


def scores_for(leads, now, bridge=None, config_path=None):
    """{lead_id: composite} from the sanctioned Node scorer, or None (replies then omit scores). Before the first
    save from the board's Ranking settings there is no config file: the scorer's own defaults apply."""
    try:
        bridge = bridge or _bridge()
        path = Path(config_path or config.scoring_config_path())
        cfg = json.loads(path.read_text(encoding="utf-8")) if path.exists() else _scoring_defaults()
        out = bridge.rank([bridge.lead_to_view(l) for l in leads], cfg, int(now.timestamp() * 1000))
        return {r["lead_id"]: r["composite"] for r in out["ranked"]}
    except Exception as e:  # noqa: BLE001 - a missing score never blocks a reply (spec §5.2)
        log(f"scorer unavailable: {type(e).__name__}: {e}"[:ERR_MAX])
        return None


# ---- the run --------------------------------------------------------------------------------

def prefetch_jds(store, lead_ids, batch, fetch=None) -> int:
    """Addendum item 1: Python reads Workday JDs (the headless run has no curl) into
    .checkpoints/<batch>/jd/<lead_id>.txt, where triage rung 2 reads them. fleet_merge and fleet_wave read only
    top-level <batch>/*.json, so the jd/ subdir is never mistaken for a checkpoint. Never raises."""
    fetch = fetch or ats_fetch.fetch_workday_jd
    jd_dir, n = paths.checkpoints_dir() / batch / "jd", 0
    for lid in lead_ids:
        try:
            lead = store.read(lid)
            urls = [lead.get("canonical_url")] + list((lead.get("intake") or {}).get("jd_links") or [])
            url = next((u for u in urls if ats_fetch.is_workday(u)), None)
            if url is None:
                continue
            text = fetch(url, log=log)
            if not text:
                continue
            jd_dir.mkdir(parents=True, exist_ok=True)
            tmp = jd_dir / f"{lid}.txt.tmp"
            tmp.write_text(text, encoding="utf-8")
            os.replace(tmp, jd_dir / f"{lid}.txt")
            n += 1
        except Exception as e:  # noqa: BLE001 - triage falls back to its own fetch ladder
            log(f"jd prefetch failed for {lid}: {type(e).__name__}: {e}"[:ERR_MAX])
    return n


def _status(store, lid):
    try:
        return (store.read(lid).get("triage") or {}).get("triage_status", "pending")
    except LeadStoreError:
        return None


def _unmerged(store, result, batch) -> list:
    """Leads result.json reports as processed whose lead files Stage 4 never updated (fleet_merge did not run).
    A triaged lead must be complete; a failed lead with a checkpoint must no longer be pending (fleet_merge reads
    checkpoint files only, so a failed lead without one stays pending even after a real merge)."""
    bid = str(result.get("batch_id") or batch)
    ckpt = paths.checkpoints_dir() / bid
    out = [lid for lid in result.get("triaged") or [] if _status(store, lid) not in ("complete", None)]
    out += [lid for lid in result.get("failed") or []
            if (ckpt / f"{lid}.json").exists() and _status(store, lid) == "pending"]
    # Review fix 3: a retried lead is already `failed` from an earlier batch, so "not pending" proves nothing; its new
    # checkpoint is merged only once the lead's triage.batch_id names this batch.
    for lid in (result.get("triaged") or []) + (result.get("failed") or []):
        if lid not in out and (ckpt / f"{lid}.json").exists() and _batch_of(store, lid) not in (bid, None):
            out.append(lid)
    return out


def _batch_of(store, lid):
    try:
        return (store.read(lid).get("triage") or {}).get("batch_id") or ""
    except LeadStoreError:
        return None


def _should_reply(st, night, final, pending) -> bool:
    """At most one partial reply per night and one final reply ever; a final may follow a same-night partial."""
    if st.get("final"):
        return False
    if final:
        return True
    if night in (st.get("replied_nights") or []):
        return False
    last = st.get("last_pending")
    return last is None or set(last) != set(pending)


def run(*, slack, store, ledger, user_id, channel, now, post=True, runner=run_headless, scorer=scores_for,
        sleep=time.sleep, run_dir=None, limit=MAX_LEADS, prefs=None, clock=None, out=None, jd_fetch=None,
        quiet_hours=("22:00", "07:00")) -> dict:
    """`now` is the run's start. `out` (optional, caller-owned) is filled in place as the run progresses, so the
    caller still knows the batch it started if a later step raises (ruling C2)."""
    clock = clock or _utcnow
    night = si.night_of(now)
    oldest = (now - timedelta(days=si.WINDOW_DAYS)).timestamp()
    msgs = slack.history(f"{oldest:.6f}")
    state = ledger.state()
    work = si.work(msgs, state, user_id)
    summary = out if out is not None else {}
    summary.update({"messages": len(work), "ingested": 0, "queued": 0, "replies": 0,
                    "stale": si.stale(state, oldest), "batch": None, "deferred": False, "run": None})

    # 1. intake (new messages, and new links in edited messages - plan D4)
    for m in work:
        urls, note = si.parse_message(m.get("text", ""))
        known = set((state.get(m["ts"]) or {}).get("by_url") or {})
        new = [u for u in urls if u not in known]
        if (state.get(m["ts"]) or {}).get("ingested") and not new:
            continue
        rep = leads_intake.ingest(new, "slack", store, note=note or None,
                                  slack={"channel": channel, "ts": m["ts"]}) if new else {"by_url": {}}
        lids = [lid for e in rep["by_url"].values() for lid in e["lead_ids"]]
        pre = [lid for lid in lids if _status(store, lid) == "complete"]
        pre_failed = [lid for lid in lids if _status(store, lid) == "failed"]   # a re-drop retries it once
        ledger.append({"ts": m["ts"], "channel": channel, "state": "ingested", "kind": None, "night": night,
                       "by_url": rep["by_url"], "pre_triaged": pre, "pre_failed": pre_failed,
                       "at": now.isoformat()})
        summary["ingested"] += 1
    state = ledger.state()

    # 2-4. queue and headless triage
    labels = screen_labels(prefs if prefs is not None else load_prefs())
    retry_kinds = {}
    queue = si.build_queue(work, state, store, night, labels, attempts=ledger.attempts(), kinds=retry_kinds,
                           started=now)
    summary["queued"] = len(queue)
    result = {"ok": True, "airtable_dedupe": "ok", "skipped": True}
    if queue:
        run_dir = Path(run_dir or paths.slack_intake_dir() / now.strftime("%Y%m%d-%H%M%S"))
        run_dir.mkdir(parents=True, exist_ok=True)
        summary["batch"] = batch_id_for(run_dir)
        ids_path = run_dir / "ids.txt"
        ids_path.write_text("\n".join(queue) + "\n", encoding="utf-8")
        # Addendum item 2: a retry counts when it is queued, not when a checkpoint lands, so a retry whose subagent
        # dies still spends its attempt. Only leads inside tonight's cap are queued for real (fleet_wave --limit).
        queued_retries = [{"ts": ts, "channel": channel, "state": "retry", "kind": kind, "lead_id": lid,
                           "night": night, "batch": summary["batch"], "at": now.isoformat()}
                          for lid in queue[:limit] for kind, ts in retry_kinds.get(lid, [])]
        for row in queued_retries:
            ledger.append(row)
        summary["retries"] = sum(1 for lid in queue[:limit] if lid in retry_kinds)
        run_ok = False
        try:
            summary["jd_prefetched"] = prefetch_jds(store, queue, summary["batch"], fetch=jd_fetch)
            try:
                result = runner(ids_path, run_dir / "result.json", limit)
            except Exception as e:  # noqa: BLE001 - e.g. `claude` not on PATH: the replies say "retrying next run"
                result = {"ok": False, "error": f"headless run raised {type(e).__name__}: {e}"[:ERR_MAX]}
            if result.get("ok") and _unmerged(store, result, summary["batch"]):
                result = {**result, "ok": False, "error": "batch not merged"}
            run_ok = bool(result.get("ok"))
        finally:
            if not run_ok:
                # Controller ruling on concern 1, in a finally (review fix 1a) so a crash here refunds too: a headless
                # run that failed as a whole spent no retry, so each lead keeps its retry and its line stays non-final.
                # A process killed outright is refunded at the next start (_refund_dead_batches, review fix 1b).
                for row in queued_retries:
                    ledger.append({**row, "state": "refund", "at": clock().isoformat()})
    summary["run"] = {k: result.get(k) for k in ("ok", "error", "airtable_dedupe", "carried", "stopped", "skipped")}

    leads = {}
    for st in (state.get(m["ts"]) or {} for m in work):
        for e in (st.get("by_url") or {}).values():
            for lid in e.get("lead_ids") or []:
                try:
                    leads[lid] = store.read(lid)
                except LeadStoreError:
                    pass
    scores = scorer(list(leads.values()), now) if leads else {}
    attempts = ledger.attempts()
    auto_left = {lid for lid in leads if si.auto_retry_left(lid, attempts)}

    def ctx_for(m):
        st = state.get(m["ts"]) or si.blank_state()
        retry = {lid for lid, at in (st.get("pre_failed") or {}).items()
                 if si.redrop_retry_due(leads.get(lid), m["ts"], lid, at, attempts)}
        return sr.Ctx(leads=leads, scores=scores, run_ok=bool(result.get("ok")),
                      airtable_skipped=result.get("airtable_dedupe") == "skipped", screen_labels=labels,
                      pre_triaged=set(st.get("pre_triaged") or []), retry=retry, auto_retry=auto_left)

    # 5. replies, after quiet hours
    if post:
        t = clock()
        wait = si.quiet_wait_seconds(t, *quiet_hours)   # measured after triage, not at start
        elapsed = (t - now).total_seconds()
        if wait and elapsed + wait > DEFER_TOTAL_S:
            # Ruling C9: no replied rows, so the next 05:30 run posts after its own 07:00 hold.
            summary["deferred"] = True
            log(f"replies deferred: {elapsed / 3600:.1f} h elapsed, quiet hours end in {wait / 3600:.1f} h")
            return summary
        if wait:
            sleep(wait)
    for m in work:
        st = state.get(m["ts"]) or si.blank_state()
        ctx = ctx_for(m)
        text, final, pending = sr.render(st.get("by_url") or {}, ctx, only=st.get("last_pending"))
        if not _should_reply(st, night, final, pending):
            continue
        if not post:
            print(f"WOULD REPLY in thread {m['ts']}:\n{text}\n")
            continue
        row = {"ts": m["ts"], "channel": channel, "state": "replied", "kind": "final" if final else "partial",
               "night": night, "pending": pending, "at": now.isoformat()}
        if final and any(r.get("bot_id") and si.FINAL_TOKEN in (r.get("text") or "") for r in slack.replies(m["ts"])):
            ledger.append(row)   # a final reply is already in the thread (crash after post): record, don't repost
            continue
        slack.post(text, thread_ts=m["ts"])
        ledger.append(row)
        summary["replies"] += 1
    return summary


def _record_health(health, night, summary) -> None:
    """Ruling C6: only a run whose headless triage succeeded counts as a successful night.
    Ruling C2: a failed run's batch is merged by the next run's prompt, and forgotten once a headless run succeeds."""
    r = summary.get("run") or {}
    if r.get("ok", True):
        health.success(night)
        if summary.get("batch"):
            health.clear_unmerged()
    elif summary.get("batch"):
        health.add_unmerged(summary["batch"])


RUN_STATUS = "run-status.json"


def _write_status(d: dict) -> None:
    """slack-intake/run-status.json, atomically (tmp + replace). Never raises: the guard must not need a disk."""
    try:
        path = paths.slack_intake_dir() / RUN_STATUS
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_name(path.name + ".tmp")
        tmp.write_text(json.dumps(d, indent=2), encoding="utf-8")
        os.replace(tmp, path)
    except Exception:  # noqa: BLE001
        pass


def _read_status() -> dict:
    try:
        d = json.loads((paths.slack_intake_dir() / RUN_STATUS).read_text(encoding="utf-8"))
        return d if isinstance(d, dict) else {}
    except Exception:  # noqa: BLE001
        return {}


def _safe_log(line: str) -> None:
    try:
        log(line)
    except Exception:  # noqa: BLE001
        pass


def _unfinished_note(prev: dict, now: datetime):
    """Addendum item 3: a run-status still `running` after 3 h means that run died (crash or power loss)."""
    started = prev.get("started") if prev.get("state") == "running" else None
    try:
        t = datetime.fromisoformat(started)
    except (TypeError, ValueError):
        return None
    t = t if t.tzinfo else t.replace(tzinfo=timezone.utc)
    if (now - t).total_seconds() <= STALE_LOCK_S:
        return None
    when = t.astimezone(si.ET).strftime("%Y-%m-%d %H:%M")
    return f"the previous run (started {when} {si.ET.key}) never finished: crash or power loss"


def _load_slack_config() -> dict:
    return config.module("slack_intake")


def _crash_alert(env: dict, night: str, note: str) -> None:
    """One #jobs alert for an unhandled crash, when the channel and token are known. Never raises."""
    try:
        if env.get("no_post") or not env.get("channel") or not env.get("token"):
            return
        health = env.get("health")
        if health is not None and not health.can_alert(night):
            return
        slack = env.get("slack") or Slack(env["channel"], token=env["token"])
        slack.post(si.failure_text([note], []))
        if health is not None:
            health.alerted(night)
    except Exception as e:  # noqa: BLE001
        _safe_log(f"crash alert not posted: {type(e).__name__}: {e}"[:ERR_MAX])


def _args(argv):
    ap = argparse.ArgumentParser()
    ap.add_argument("--no-post", action="store_true", help="dry run: print replies, post nothing")
    ap.add_argument("--limit", type=int, default=MAX_LEADS)
    return ap.parse_args(argv)


def _batch_result_ok(batch: str) -> bool:
    """The batch's run dir (slack-intake/<name>, batch 'slack-<name>') holds a result.json that is not stopped."""
    name = batch[len("slack-"):] if batch.startswith("slack-") else batch
    try:
        res = json.loads((paths.slack_intake_dir() / name / "result.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return False
    return isinstance(res, dict) and not res.get("stopped")


def _refund_dead_batches(ledger, health, dead: bool, now) -> list:
    """Review fix 1b: a run killed outright (no finally) leaves retry rows behind. At start, refund the unrefunded
    retries of every batch in health.unmerged() - and, after a dead `running` status, of every batch - whose run
    left no ok result.json."""
    rows = ledger.unrefunded_retries()
    batches = {r.get("batch") for r in rows if r.get("batch")}
    if not dead:
        batches &= set(health.unmerged())
    refunded = sorted(b for b in batches if not _batch_result_ok(b))
    for r in rows:
        if r.get("batch") in refunded:
            ledger.append({**r, "state": "refund", "at": now.isoformat()})
    return refunded


def main(argv=None) -> int:
    """Addendum item 3: the whole run sits inside a guard. A heartbeat goes to run-status.json before anything else;
    every exit writes its outcome there; an unhandled exception anywhere is recorded, logged, alerted once in #jobs
    (when the channel and token are known) and exits non-zero. Only argparse's SystemExit is an argument exit; one
    raised later is a crash (review fix 6). A run skipped on a busy lock leaves the live run's record alone (fix 7)."""
    started = _utcnow()
    night = si.night_of(started)
    prev = _read_status()
    _write_status({"state": "running", "started": started.isoformat(), "pid": os.getpid(), "night": night})
    env = {"channel": None, "token": None, "health": None, "slack": None, "no_post": False}

    def finish(state, rc, notes):
        _write_status({"state": state, "started": started.isoformat(), "finished": _utcnow().isoformat(),
                       "rc": rc, "notes": notes, "night": night})

    def restore_prev():
        """Put the previous record back, but only while the file still holds this run's own heartbeat (a run that
        finished meanwhile keeps its outcome)."""
        cur = _read_status() or {}
        if cur.get("pid") == os.getpid() and cur.get("started") == started.isoformat() and cur.get("state") == "running":
            _write_status(prev)

    try:
        a = _args(argv)
    except SystemExit as e:   # argparse: --help (0) or bad arguments (2)
        code = e.code if isinstance(e.code, int) else (0 if e.code is None else 2)
        if (prev or {}).get("state") == "running":
            restore_prev()   # keep a dead run's record: the next start's refund sweep depends on it
        else:
            finish("skipped" if code == 0 else "failed", code, [] if code == 0 else ["bad command-line arguments"])
        raise
    try:
        out = _main(a, env, prev, started, night)
    except (Exception, SystemExit) as e:  # noqa: BLE001 - the silent-failure guard
        note = f"unhandled {type(e).__name__}: {e}"[:ERR_MAX]
        finish("failed", 1, [note])
        _safe_log(json.dumps({"night": night, "rc": 1, "crash": note}))
        _crash_alert(env, night, note)
        return 1
    if out.get("keep_status"):
        restore_prev()   # busy lock: the live run's `running` record stays as it was
    else:
        finish(out["state"], out["rc"], out["notes"])
    return out["rc"]


def _main(a, env, prev, now, night) -> dict:
    env["no_post"] = a.no_post
    cfg = _load_slack_config()
    channel, user_id = cfg.get("channel_id"), cfg.get("user_id")
    env["channel"] = channel
    if not (cfg.get("enabled") and config.enabled("ai_triage")):
        print("modules.slack_intake and modules.ai_triage must both be enabled in config.yaml")
        return {"state": "failed", "rc": 2, "notes": ["slack_intake / ai_triage not enabled"]}
    if not channel or not user_id:
        print("modules.slack_intake.channel_id / user_id not set in config.yaml")
        return {"state": "failed", "rc": 2, "notes": ["channel_id / user_id not set in config.yaml"]}
    si.set_timezone(cfg.get("timezone"))
    env["token"] = _token()
    base = paths.slack_intake_dir()
    health = env["health"] = si.Health(base / "state.json")
    slack = env["slack"] = Slack(channel, token=env["token"])
    notes, summary, rc = [], {}, 0
    gap = health.gap_nights(night)   # read before tonight's run records its own result
    unfinished = _unfinished_note(prev, now)
    lock = si.acquire_lock(base / "intake.lock", now, STALE_LOCK_S)
    if lock == "busy":
        log("skipped: another run holds the lock")
        return {"state": "skipped", "rc": 0, "notes": ["another run holds the lock"], "keep_status": bool(prev)}
    if unfinished:
        notes.append(unfinished)   # covers the stale lock too: one note for one dead run
    elif lock == "stale":
        notes.append("the previous run's lock was older than 3 h and was broken")
    ledger = si.Ledger(paths.leads_dir() / si.LEDGER_NAME)
    try:
        refunded = _refund_dead_batches(ledger, health, unfinished is not None, now)
        if refunded:
            log(f"refunded the retries of dead batch(es): {', '.join(refunded)}"[:ERR_MAX])
        runner = functools.partial(run_headless, merge_first=tuple(health.unmerged()))
        summary = run(slack=slack, store=LeadStore(), ledger=ledger,
                      user_id=user_id, channel=channel, now=now, post=not a.no_post, limit=a.limit, runner=runner,
                      out=summary, quiet_hours=tuple(cfg.get("quiet_hours") or ("22:00", "07:00")))
        _record_health(health, night, summary)
    except Exception as e:  # noqa: BLE001 - one Slack message, then a non-zero exit for Task Scheduler
        notes.append(f"{type(e).__name__}: {e}"[:ERR_MAX])
        rc = 1
        if summary.get("batch") and not (summary.get("run") or {}).get("ok"):
            health.add_unmerged(summary["batch"])   # a started batch whose run never finished ok (ruling C2)
    finally:
        si.release_lock(base / "intake.lock")
    # Spec §6 "no successful run two nights in a row": tonight failed AND the night before had no success either.
    # Addendum item 3: a system that has never succeeded alerts on its first failed night too.
    tonight_ok = rc == 0 and (summary.get("run") or {}).get("ok", True)
    if not tonight_ok and gap is None:
        why = (summary.get("run") or {}).get("error") or "see runs.log"
        notes.insert(0, f"intake has not completed a successful run yet (tonight: {why})"[:ERR_MAX])
    elif not tonight_ok and gap >= 2:
        last = (datetime.fromisoformat(night) - timedelta(days=gap)).date().isoformat()
        notes.insert(0, f"intake had not run successfully since {last} (PC off?)")
    log(json.dumps({"night": night, "rc": rc, "notes": notes, **{k: v for k, v in summary.items() if k != "stale"},
                    "stale": summary.get("stale", [])}))
    if notes and not a.no_post and health.can_alert(night):
        try:
            slack.post(si.failure_text(notes, summary.get("stale", [])))
            health.alerted(night)
        except Exception as e:  # noqa: BLE001
            log(f"failure message not posted: {type(e).__name__}: {e}"[:ERR_MAX])
    return {"state": "ok" if tonight_ok else "failed", "rc": rc, "notes": notes}


if __name__ == "__main__":
    sys.exit(main())
