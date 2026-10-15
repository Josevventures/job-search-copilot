"""Slack #jobs intake: pure helpers for reading drops and keeping the processing ledger. No network, no LLM.
Design: docs/ARCHITECTURE.md, "Slack intake"."""
import html as htmllib
import json
import os
import re
from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

from lib.lead_store import LeadStoreError

LEDGER_NAME = "_slack_intake.jsonl"   # beside _skips.jsonl in the Leads dir; LeadStore.list() globs *.json only
WINDOW_DAYS = 14
FINAL_TOKEN = "[intake:final]"
LEAD_ROWS = ("retry", "refund")
NOTE_MAX = 300

_LINK = re.compile(r"<(https?://[^>|]+)(?:\|[^>]*)?>|(https?://[^\s<>|]+)")
_MARKUP = re.compile(r"<[^>]*>")
_TRAIL = ".,;:!?)]}'\""


def is_drop(msg: dict, user_id: str) -> bool:
    """A top-level message the user wrote. Thread replies (the bot's and the user's own) are never drops."""
    return (msg.get("user") == user_id and not msg.get("subtype") and not msg.get("bot_id")
            and msg.get("thread_ts") in (None, msg.get("ts")))


def parse_message(text: str) -> tuple[list[str], str]:
    text = text or ""
    urls = []
    for m in _LINK.finditer(text):
        u = htmllib.unescape(m.group(1) or m.group(2)).rstrip(_TRAIL)
        if u not in urls:
            urls.append(u)
    rest = _MARKUP.sub(" ", text)
    rest = re.sub(r"https?://\S+", " ", rest)
    note = " ".join(htmllib.unescape(rest).split())[:NOTE_MAX]
    return urls, note


def blank_state():
    return {"ingested": False, "by_url": {}, "pre_triaged": [], "pre_failed": {}, "ingested_night": None,
            "final": False, "replied_nights": [], "last_pending": None}


class Ledger:
    """Append-only JSONL, one row per state change: ingested -> replied(partial)* -> replied(final).
    Two per-lead row kinds ride along (keyed by lead_id, ignored by state()): `retry` (kind auto|redrop), written
    when a failed lead is queued for a retry (addendum item 2), and `refund`, when the run that queued it failed."""

    def __init__(self, path):
        self.path = Path(path)

    def rows(self) -> list[dict]:
        try:
            text = self.path.read_text(encoding="utf-8")
        except FileNotFoundError:
            return []
        out = []
        for line in text.splitlines():
            try:
                r = json.loads(line)
            except ValueError:
                continue
            if isinstance(r, dict) and r.get("ts") and r.get("state"):
                out.append(r)
        return out

    @staticmethod
    def _key(row: dict) -> tuple:
        # An ingested row also keys on its link set: an edit that adds links the same night is a new row,
        # a repeat of the same links is not (task 7 ruling on the same-night re-run).
        links = tuple(sorted(row.get("by_url") or {})) if row["state"] == "ingested" else None
        if row["state"] in LEAD_ROWS:   # one attempt / heads-up per lead (and per message for a re-drop)
            ts = row["ts"] if row.get("kind") == "redrop" else None
            batch = row.get("batch")   # a refunded retry may be queued again
            return ts, row["state"], row.get("kind"), batch, row.get("lead_id")
        return row["ts"], row["state"], row.get("kind"), row.get("night"), links

    def append(self, row: dict) -> bool:
        key = self._key(row)
        if any(self._key(r) == key for r in self.rows()):
            return False
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with open(self.path, "a", encoding="utf-8", newline="\n") as f:
            f.write(json.dumps(row, ensure_ascii=False) + "\n")
        return True

    def state(self) -> dict:
        out = {}
        for r in self.rows():
            if r["state"] in LEAD_ROWS:
                continue
            s = out.setdefault(r["ts"], blank_state())
            if r["state"] == "ingested":
                s["ingested"] = True
                if s["last_pending"] is not None:  # links added by an edit after a partial reply: report them too
                    s["last_pending"] += [lid for e in (r.get("by_url") or {}).values()
                                          for lid in e.get("lead_ids") or []]
                s["by_url"].update(r.get("by_url") or {})
                s["pre_triaged"] += [x for x in r.get("pre_triaged") or [] if x not in s["pre_triaged"]]
                for x in r.get("pre_failed") or []:   # a re-drop of a failed lead: retry due since this row
                    s["pre_failed"].setdefault(x, r.get("at"))
                s["ingested_night"] = r.get("night")
            elif r["state"] == "replied":
                s["replied_nights"].append(r.get("night"))
                if r.get("kind") == "final":
                    s["final"] = True
                else:
                    s["last_pending"] = list(r.get("pending") or [])
        return out

    def unrefunded_retries(self) -> list:
        """Retry rows with no refund row for their batch (review fix 1b)."""
        rows = [r for r in self.rows() if r["state"] in ("retry", "refund") and r.get("lead_id")]
        refunded = {self._key({**r, "state": "retry"}) for r in rows if r["state"] == "refund"}
        return [r for r in rows if r["state"] == "retry" and self._key(r) not in refunded]

    def attempts(self) -> dict:
        """Retries spent: {"auto": {lead_id}, "redrop": {(message ts, lead_id)}}. A retry row counts unless a `refund`
        row for the same batch says its headless run failed as a whole (controller ruling on concern 1)."""
        rows = [r for r in self.rows() if r["state"] in ("retry", "refund") and r.get("lead_id")]
        refunded = {self._key({**r, "state": "retry"}) for r in rows if r["state"] == "refund"}
        out = {"auto": set(), "redrop": set()}
        for r in rows:
            if r["state"] != "retry" or self._key(r) in refunded:
                continue
            if r.get("kind") == "auto":
                out["auto"].add(r["lead_id"])
            elif r.get("kind") == "redrop":
                out["redrop"].add((r["ts"], r["lead_id"]))
        return out


def work(messages, state, user_id) -> list[dict]:
    out = [m for m in messages if is_drop(m, user_id) and not (state.get(m["ts"]) or {}).get("final")]
    return sorted(out, key=lambda m: float(m["ts"]))


def stale(state, oldest_ts: float) -> list[str]:
    return sorted((ts for ts, s in state.items() if not s.get("final") and float(ts) < oldest_ts), key=float)


def _ts(iso):
    try:
        d = datetime.fromisoformat(iso)
    except (TypeError, ValueError):
        return None
    return d if d.tzinfo else d.replace(tzinfo=timezone.utc)


def retry_due(lead, since) -> bool:
    """A lead that was triage-failed when the user re-dropped it (at `since`) is due one retry until it is triaged
    again: failed, and not triaged after the re-drop. A retry that fails again is final, so there is no loop."""
    t = (lead or {}).get("triage") or {}
    if t.get("triage_status") != "failed":
        return False
    done, start = _ts(t.get("triaged_at")), _ts(since)
    return done is None or start is None or done <= start


def _skipped_by_triage(lead, labels) -> bool:
    """Same exclusions as fleet_wave.select_leads: screened out or already applied (same role in Airtable)."""
    t, i = lead.get("triage") or {}, lead.get("intake") or {}
    return ((t.get("airtable_match") or {}).get("relation") == "same_role"
            or (i.get("screen") or {}).get("label") in labels)


def _failed_before(lead, started) -> bool:
    """Review fix 2: the automatic retry is due on any run after the one that failed the lead, judged by the failure
    time against this run's start (not by the ET date of a merge that may land after midnight)."""
    t = _ts(((lead or {}).get("triage") or {}).get("triaged_at"))
    return started is None or t is None or t < started


def auto_retry_left(lid, attempts) -> bool:
    """Addendum item 2: every failed lead gets ONE automatic retry, counted when it is queued."""
    return lid not in ((attempts or {}).get("auto") or set())


def redrop_retry_due(lead, ts, lid, since, attempts) -> bool:
    """A re-drop (message `ts`) of a lead that was failed when ingested: one retry per re-drop message."""
    return retry_due(lead, since) and (ts, lid) not in ((attempts or {}).get("redrop") or set())


def _due_retries(lid, lead, work_msgs, state, started, attempts) -> list:
    if ((lead.get("triage") or {}).get("triage_status")) != "failed":
        return []
    due = []
    if auto_retry_left(lid, attempts) and _failed_before(lead, started):   # never the run that failed it
        home = next((m["ts"] for m in work_msgs
                     for e in ((state.get(m["ts"]) or {}).get("by_url") or {}).values()
                     if lid in (e.get("lead_ids") or [])), None)
        due.append(("auto", home))
    for m in work_msgs:
        since = ((state.get(m["ts"]) or {}).get("pre_failed") or {}).get(lid)
        if since is not None and redrop_retry_due(lead, m["ts"], lid, since, attempts):
            due.append(("redrop", m["ts"]))
    return due


def build_queue(work_msgs, state, store, night: str, labels=(), attempts=None, kinds=None,
                started=None) -> list[str]:
    """Leads of unfinished messages that need triage - pending ones, plus failed ones due a retry (one automatic
    retry per lead from the night after it failed, and one per re-drop message) - minus those already screened out
    or matched to Airtable. Earlier nights first (carried), then tonight's, message then link order.
    `kinds` (optional, caller-owned) is filled with {lead_id: [(kind, message ts), ...]} for every queued retry:
    one run spends every retry due for that lead, and the caller records them in the ledger when it queues them."""
    carried, tonight, seen = [], [], set()
    for m in work_msgs:
        s = state.get(m["ts"]) or {}
        pre = set(s.get("pre_triaged") or [])
        target = tonight if s.get("ingested_night") == night else carried
        for e in (s.get("by_url") or {}).values():
            for lid in e.get("lead_ids") or []:
                if lid in seen or lid in pre:   # queued once, even if another message also brought it
                    continue
                try:
                    lead = store.read(lid)
                except LeadStoreError:
                    continue
                if _skipped_by_triage(lead, labels):
                    continue
                status = (lead.get("triage") or {}).get("triage_status", "pending")
                due = _due_retries(lid, lead, work_msgs, state, started, attempts) if status == "failed" else []
                if status == "pending" or due:
                    seen.add(lid)
                    target.append(lid)
                    if due and kinds is not None:
                        kinds[lid] = due
    return carried + tonight


DEFAULT_TZ = "America/New_York"
ET = ZoneInfo(DEFAULT_TZ)   # the configured zone (modules.slack_intake.timezone); needs tzdata on Windows


def set_timezone(name: str | None) -> None:
    """Nights, quiet hours and alert timestamps all use one zone. Called once at startup from the config."""
    global ET
    ET = ZoneInfo(name or DEFAULT_TZ)


def night_of(now: datetime) -> str:
    return now.astimezone(ET).date().isoformat()


def _hm(s: str) -> tuple[int, int]:
    h, m = s.split(":")
    return int(h), int(m)


def quiet_wait_seconds(now: datetime, start: str = "22:00", end: str = "07:00") -> float:
    """Seconds until quiet hours end (configured zone), or 0 outside them. Quiet hours wrap midnight.
    Subtracts in UTC so DST-change nights come out right."""
    local = now.astimezone(ET)
    sh, sm = _hm(start)
    eh, em = _hm(end)
    mins = local.hour * 60 + local.minute
    if not (mins >= sh * 60 + sm or mins < eh * 60 + em):
        return 0
    day = local.date() if mins < eh * 60 + em else local.date() + timedelta(days=1)
    target = datetime(day.year, day.month, day.day, eh, em, tzinfo=ET)
    return max(0.0, (target.astimezone(timezone.utc) - now.astimezone(timezone.utc)).total_seconds())


def acquire_lock(path, now: datetime, stale_s: float) -> str:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
    except FileExistsError:
        try:
            started = datetime.fromisoformat(path.read_text(encoding="utf-8").strip())
        except (OSError, ValueError):
            started = None
        if started is None or (now - started).total_seconds() > stale_s:
            path.write_text(now.isoformat(), encoding="utf-8")
            return "stale"
        return "busy"
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        f.write(now.isoformat())
    return "ok"


def release_lock(path) -> None:
    Path(path).unlink(missing_ok=True)


class Health:
    """slack-intake/state.json: last successful night and last night an alert was posted (one alert per night)."""

    def __init__(self, path):
        self.path = Path(path)

    def _read(self) -> dict:
        try:
            return json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {}

    def _write(self, d: dict) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(json.dumps(d, indent=2), encoding="utf-8")

    def gap_nights(self, night: str):
        last = self._read().get("last_success")
        if not last:
            return None
        return (datetime.fromisoformat(night) - datetime.fromisoformat(last)).days

    def success(self, night: str) -> None:
        self._write({**self._read(), "last_success": night})

    def can_alert(self, night: str) -> bool:
        return self._read().get("last_alert") != night

    def alerted(self, night: str) -> None:
        self._write({**self._read(), "last_alert": night})

    # Batches of failed headless runs whose checkpoints the next run merges first (controller ruling C2).
    def unmerged(self) -> list:
        return list(self._read().get("unmerged_batches") or [])

    def add_unmerged(self, batch: str) -> None:
        cur = self.unmerged()
        self._write({**self._read(), "unmerged_batches": cur + ([batch] if batch not in cur else [])})

    def clear_unmerged(self) -> None:
        self._write({**self._read(), "unmerged_batches": []})


def failure_text(notes: list, stale_ts: list) -> str:
    t = f"Jobs intake failed last night: {'; '.join(notes)}. Your links are safe and will be retried."
    if stale_ts:
        t += f" Older than 14 days and never finished (messages): {', '.join(stale_ts)}."
    return t
