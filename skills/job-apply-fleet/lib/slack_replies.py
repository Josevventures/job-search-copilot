"""Slack #jobs thread replies: lead files -> one line per link. Pure; no network.
Design: docs/ARCHITECTURE.md, "Slack intake" (plan D1: score only, no board position)."""
from dataclasses import dataclass, field

from lib.slack_intake import FINAL_TOKEN

MAX_LINES = 15
SEP = " · "
PENDING_KINDS = {"queued", "unfinished", "retrying"}
REASON_MAX = 80
REDROP = "Re-drop with a cleaner link or run directly via /job-apply."


def parse_failure(reason, name=None) -> str:
    """The final failed-scrape line (addendum item 2), for a lead (`name`) or an ingest failure (no name)."""
    who = f"{name} — " if name else ""
    return f"⚠️ {who}Could not parse job description ({(reason or 'unknown')[:REASON_MAX]}). {REDROP}"


@dataclass
class Ctx:
    leads: dict
    scores: dict | None
    run_ok: bool
    airtable_skipped: bool
    screen_labels: list
    pre_triaged: set = field(default_factory=set)
    retry: set = field(default_factory=set)   # failed leads the user re-dropped, due a retry: pending until triaged
    auto_retry: set = field(default_factory=set)   # failed leads whose one automatic retry is still to come


def _name(lead):
    t = lead.get("triage") or {}
    return SEP.join(p for p in (t.get("company"), t.get("role_title")) if p) or lead.get("canonical_url", "?")


def _score(lead, ctx):
    s = (ctx.scores or {}).get(lead.get("lead_id"))
    return f"{SEP}score {s}" if s is not None else ""


def classify(lead, ctx) -> tuple[str, str]:
    t, i = lead.get("triage") or {}, lead.get("intake") or {}
    screen = i.get("screen") or {}
    status, route, name = t.get("triage_status", "pending"), t.get("route"), _name(lead)
    if route == "screened" or screen.get("label") in ctx.screen_labels:
        label = (screen.get("label") or "screened").replace("_", " ")
        reason = screen.get("reason")
        return "screened", f"🚫 screened out: {label}" + (f' — "{reason}"' if reason else "")
    if route == "airtable" or (t.get("airtable_match") or {}).get("relation") == "same_role":
        return "airtable", f"↩️ already in your Airtable applications: {name}"
    if status == "pending" or lead.get("lead_id") in ctx.retry:
        if ctx.run_ok:
            return "queued", f"⏳ {name}{SEP}queued (over tonight's cap)"
        return "unfinished", f"⏳ {name}{SEP}triage didn't finish, retrying next run"
    if status == "failed":
        reason = (t.get("triage_error") or "unknown")[:REASON_MAX]
        if lead.get("lead_id") in ctx.auto_retry:
            return "retrying", f"⏳ {name} — couldn't read it tonight ({reason}); retrying once tomorrow night"
        return "failed", parse_failure(reason, name)
    if route == "closed":
        return "closed", f"🔒 {name}{SEP}closed posting"
    if lead.get("lead_id") in ctx.pre_triaged:   # after closed/failed, so a re-drop shows its real state
        return "already", f"↩️ already on board: {name}{_score(lead, ctx)}"
    if t.get("jd_read") is False:
        return "unread", f"⚠️ {name}{SEP}couldn't read the posting (login wall / fetch error) — see the board card"
    return "board", "✅ " + SEP.join(p for p in (name, t.get("location")) if p) + _score(lead, ctx)


def _roundup(items, ctx) -> str:
    kinds = [k for k, _, _ in items]
    board = [l for k, _, l in items if k in ("board", "already")]
    line = f"📋 roundup ({len(items)} roles): {len(board)} on board"
    scored = [(ctx.scores or {}).get(l["lead_id"]) for l in board]
    if any(s is not None for s in scored):
        s, top = max(((s, l) for s, l in zip(scored, board) if s is not None), key=lambda x: x[0])
        line += f" — top: {_name(top)} (score {s})"
    for label, ks in (("screened", ("screened",)), ("queued", ("queued", "unfinished")),
                      ("retrying tomorrow", ("retrying",)),
                      ("closed", ("closed",)), ("failed", ("failed",)), ("unreadable", ("unread",)),
                      ("already in Airtable", ("airtable",))):
        n = sum(kinds.count(k) for k in ks)
        if n:
            line += f"; {n} {label}"
    return line


def render(by_url: dict, ctx: Ctx, only=None) -> tuple[str, bool, list]:
    if not by_url:
        return f"No link found in this message. {FINAL_TOKEN}", True, []
    body, pending, any_scored_kind = [], [], False
    for e in by_url.values():
        lids = e.get("lead_ids") or []
        if only is not None and not set(lids) & set(only):
            continue
        if e.get("status") == "failed":
            body.append(parse_failure(e.get("error")))
            continue
        if not lids and e.get("status") == "previously_skipped":
            body.append("⏭️ skipped earlier on the Jobs tab, not re-added")
            continue
        items = []
        for lid in lids:
            lead = ctx.leads.get(lid)
            if lead is None:
                body.append(f"❌ lead file missing: {lid}")
                continue
            k, line = classify(lead, ctx)
            items.append((k, line, lead))
            if k in PENDING_KINDS:
                pending.append(lid)
            any_scored_kind |= k in ("board", "already")
        if e.get("roundup") and items:
            body.append(_roundup(items, ctx))
        else:
            body += [line for _, line, _ in items]
    final = not pending
    footer = []
    if ctx.airtable_skipped:
        footer.append("Airtable dedupe skipped tonight.")
    if ctx.scores is None and any_scored_kind:
        footer.append("Scores on the board (scorer unavailable).")
    room = MAX_LINES - 1 - len(footer)
    if len(body) > room:
        body = body[:room - 1] + [f"…and {len(body) - room + 1} more. Full list on the Jobs board."]
    header = f"Intake done {FINAL_TOKEN}" if final else "Intake update — some roles are still queued"
    return "\n".join([header] + body + footer), final, pending
