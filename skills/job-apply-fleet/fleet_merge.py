"""Stage 4 of /job-apply-fleet: validate checkpoints, write triage group, assign routes, summarize. No LLM."""
import argparse
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from lib import paths  # noqa: E402
from lib.canonical import canonicalize  # noqa: E402
from lib.checkpoint import TRIAGE_KEYS_FROM_CHECKPOINT, validate  # noqa: E402
from lib.lead_store import LeadStore, LeadStoreError  # noqa: E402
from lib.prefs import load as load_prefs, screen_labels  # noqa: E402
from lib.routing import route  # noqa: E402

V1_CLEARED = {"bucket": None, "bucket_reason": None, "category": None, "relevance": None, "why": None}


def _airtable_fields():
    """Field ids or names for the dedupe read (modules.airtable.fields in config.yaml)."""
    from copilot import config
    f = config.module("airtable").get("fields") or {}
    return (f.get("role_url") or "Role URL", f.get("company") or "Company", f.get("role_title") or "Role Title")


def _now():
    return datetime.now(timezone.utc).isoformat()


def merge_batch(batch_id, store, prefs):
    rep = {"merged": 0, "failed": 0, "skipped_missing": 0}
    for f in sorted((paths.checkpoints_dir() / batch_id).glob("*.json")):
        if f.name.startswith("_"):
            continue
        try:
            d = json.loads(f.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            d = {"lead_id": f.stem}
        if not isinstance(d, dict):
            d = {"lead_id": f.stem}
        lid = d.get("lead_id") or f.stem
        try:
            lead = store.read(lid)
        except LeadStoreError:
            rep["skipped_missing"] += 1
            continue
        probs = validate(d)
        if probs:
            store.update(lid, "triage", {"triage_status": "failed", "batch_id": batch_id, "triaged_at": _now(),
                                         "triage_error": "malformed_checkpoint: " + "; ".join(probs)})
            rep["failed"] += 1
            continue
        if d["status"] == "failed":
            store.update(lid, "triage", {"triage_status": "failed", "batch_id": batch_id, "triaged_at": _now(),
                                         "triage_error": d.get("error") or "failed",
                                         "fetch_method": d.get("fetch_method")})
            rep["failed"] += 1
            continue
        fields = {k: d.get(k) for k in TRIAGE_KEYS_FROM_CHECKPOINT}
        rt, _reason = route(fields, lead.get("intake", {}).get("screen"), prefs)
        store.update(lid, "triage", {**fields, **V1_CLEARED, "triage_status": "complete", "triage_error": None,
                                     "batch_id": batch_id, "triaged_at": _now(), "route": rt},
                     schema_version=2)
        rep["merged"] += 1
    return rep


def screen_only(store, prefs):
    n = 0
    for lead in store.list():
        screen = lead.get("intake", {}).get("screen")
        if not screen or screen.get("label") not in screen_labels(prefs):
            continue
        if lead.get("triage", {}).get("triage_status") == "complete":
            continue
        rt, reason = route({}, screen, prefs)
        store.update(lead["lead_id"], "triage", {"triage_status": "complete", "triaged_at": _now(),
                                                 "batch_id": "screen", "route": rt, "bucket": None,
                                                 "bucket_reason": reason})
        n += 1
    return n


def _records(path):
    raw = json.loads(Path(path).read_text(encoding="utf-8"))
    role_url, company, role = _airtable_fields()
    recs = raw if isinstance(raw, list) else raw.get("records", [])
    for r in recs:
        cells = r.get("cellValuesByFieldId") or r.get("fields") or {}
        yield r.get("id"), cells.get(role_url) or cells.get("Role URL"), \
            cells.get(company) or cells.get("Company"), cells.get(role) or cells.get("Role Title")


def airtable_dedupe(store, records_path):
    index = {canonicalize(u): (rid, c, t) for rid, u, c, t in _records(records_path) if u}
    n = 0
    for lead in store.list():
        hit = index.get(lead["canonical_url"])
        if not hit or lead.get("triage", {}).get("triage_status") == "complete":
            continue
        rid, company, title = hit
        match = {"relation": "same_role", "record_id": rid, "note": f"{company} / {title}"}
        store.update(lead["lead_id"], "triage", {"triage_status": "complete", "triaged_at": _now(),
                                                 "batch_id": "airtable-dedupe", "company": company,
                                                 "role_title": title, "airtable_match": match,
                                                 "route": "airtable", "bucket": None, "bucket_reason": "already_applied"})
        n += 1
    return n


def _row(lead):
    t, i = lead.get("triage", {}), lead.get("intake", {})
    flags = []
    if t.get("jd_read") is False:
        flags.append("JD not read")
    rel = (t.get("airtable_match") or {}).get("relation")
    if rel in ("reapply_candidate", "adjacent_opening"):
        flags.append("re-apply candidate" if rel == "reapply_candidate" else "adjacent opening")
    if t.get("eligibility_note"):
        flags.append("grad program")
    apply = t.get("jd_url") or (i.get("jd_links") or [None])[0]
    apply_cell = f"[Apply]({apply})" if apply else "no posting — DM is the application"
    eng = t.get("engage") or {}
    engage_cell = f"[{eng.get('name') or 'Post'}]({eng['url']})" if eng.get("url") else ""
    where = (t.get("location") or "") + (f" · {t['pay']}" if t.get("pay") else "")
    sc = t.get("scores") or {}
    sub = (f"{(sc.get('role_fit') or {}).get('score', '?')} / {(sc.get('domain') or {}).get('score', '?')}"
           if sc else "")
    cells = [f"{t.get('company') or '?'} — {t.get('role_title') or '?'}", where, t.get("job_summary") or "",
             sub, apply_cell, engage_cell, ", ".join(flags)]
    return "| " + " | ".join(str(c).replace("|", "\\|") for c in cells) + " |"


def _group(lead):
    t = lead.get("triage", {})
    if t.get("triage_status") == "failed":
        return "attention"
    if (t.get("airtable_match") or {}).get("relation") == "same_role":
        return "airtable"
    if t.get("route") == "screened" or (lead.get("schema_version", 1) < 2 and t.get("bucket") == "skip"):
        return None
    if lead.get("schema_version", 1) < 2 or t.get("triage_status") != "complete":
        return "retriage"
    return "closed" if t.get("route") == "closed" else "board"


def write_summary(store, date):
    """Run report (ranking-engine spec D7). Alphabetical on purpose: the ranked view is the Jobs tab."""
    groups = {"board": [], "closed": [], "retriage": [], "airtable": [], "attention": []}
    for lead in store.list():
        g = _group(lead)
        if g:
            groups[g].append(lead)
    for v in groups.values():
        v.sort(key=lambda l: (str(l.get("triage", {}).get("company") or "").lower(), l["lead_id"]))
    head = ("| Role | Location / Pay | What the job is | Role fit / Domain | Apply | Engage | Flags |\n"
            "|---|---|---|---|---|---|---|")
    titles = {"board": "Board", "closed": "Closed? (confirm on the Jobs tab)", "retriage": "Needs re-triage"}
    lines = [f"# Job leads — {date}", "", "Ranked board: open the Jobs tab (rank depends on the live weights).", ""]
    for k in ("board", "closed", "retriage"):
        lines += [f"## {titles[k]} ({len(groups[k])})", "", head] + [_row(l) for l in groups[k]] + [""]
    lines += [f"## Already in Airtable ({len(groups['airtable'])})", ""] + [
        f"- {l['triage'].get('airtable_match', {}).get('note') or l['triage'].get('company')} — "
        f"record {l['triage']['airtable_match'].get('record_id')}" for l in groups["airtable"]] + [""]
    lines += [f"## Needs attention ({len(groups['attention'])})", ""] + [
        f"- {l['canonical_url']} — {l['triage'].get('triage_error')}" for l in groups["attention"]] + [""]
    out = paths.summaries_dir() / f"fleet-review-{date}.md"
    out.write_text("\n".join(lines), encoding="utf-8")
    return out


def main(argv=None):
    ap = argparse.ArgumentParser()
    g = ap.add_mutually_exclusive_group(required=True)
    g.add_argument("--batch")
    g.add_argument("--screen-only", action="store_true")
    g.add_argument("--airtable-dedupe")
    g.add_argument("--summary", action="store_true")
    ap.add_argument("--date", default=datetime.now().strftime("%Y-%m-%d"))
    a = ap.parse_args(argv)
    store, prefs = LeadStore(), load_prefs()
    if a.batch:
        print(json.dumps(merge_batch(a.batch, store, prefs)))
    elif a.screen_only:
        print(f"screened out: {screen_only(store, prefs)}")
    elif a.airtable_dedupe:
        print(f"already in Airtable: {airtable_dedupe(store, a.airtable_dedupe)}")
    else:
        print(f"wrote {write_summary(store, a.date)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
