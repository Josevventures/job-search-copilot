"""Closed-posting check (ranking engine, Phase F).

Read-only on Leads/: it writes a dated report (markdown + JSON) to the summaries dir and nothing else. The user
confirms the closed list; removal then goes through the Jobs tab's own Skip mechanics (closed_apply.py)."""
import argparse
import json
import re
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from lib import paths, prefs  # noqa: E402
from lib.fetch import fetch_status  # noqa: E402
from lib.lead_store import LeadStore  # noqa: E402

CLOSED_RE = re.compile(r"no longer accepting applications|this job is no longer available|job not found|"
                       r"position has been filled|this (?:job|position|posting) (?:has been |is )?closed|"
                       r"job (?:posting )?(?:has )?expired|no longer open", re.I)
OPEN_RE = re.compile(r"\bapply now\b|\bapply for this job\b|\bsubmit (?:your )?application\b|\beasy apply\b", re.I)


_FALLBACK_SCREEN_LABELS = ("non_job", "event", "internship_or_undergrad")
_SCREEN_LABELS = None


def _screen_labels():
    """Excluded screen labels follow preferences.md; cached; falls back to the spec's three if prefs are empty/unreadable."""
    global _SCREEN_LABELS
    if _SCREEN_LABELS is None:
        try:
            _SCREEN_LABELS = tuple(prefs.screen_labels(prefs.load())) or _FALLBACK_SCREEN_LABELS
        except Exception:
            _SCREEN_LABELS = _FALLBACK_SCREEN_LABELS
    return _SCREEN_LABELS


def eligible(lead: dict) -> bool:
    t, d = lead.get("triage") or {}, lead.get("decision") or {}
    if d.get("pending") or d.get("decision") in ("applied", "needs_help"):
        return False
    if (t.get("airtable_match") or {}).get("relation") == "same_role":
        return False
    if t.get("route") == "screened" or (lead.get("intake", {}).get("screen") or {}).get("label") in _screen_labels():
        return False
    return True   # v1 auto-skips (bucket "skip": unpaid / closed) are checked again (spec §3.3)


LD_RE = re.compile(r'<script[^>]+application/ld\+json[^>]*>(.*?)</script>', re.S | re.I)
MAIN_RE = re.compile(r"<main\b.*?</main>", re.S | re.I)


def _job_posting(text):
    for block in LD_RE.findall(text or ""):
        try:
            d = json.loads(block)
        except ValueError:
            continue
        for item in d if isinstance(d, list) else [d]:
            if isinstance(item, dict) and item.get("@type") == "JobPosting":
                return item
    return None


def verdict(status, text, today=None) -> tuple[str, str]:
    """Order matters (review minor 13): HTTP status, then structured JobPosting data, then an apply control,
    and only then closed phrases, searched in <main> when the page has one. Residual risk: a page with no <main>
    and a closed phrase in its chrome can still over-flag; the user's confirmation catches it."""
    if status in (404, 410):
        return "closed", f"HTTP {status}"
    today = today or datetime.now().strftime("%Y-%m-%d")
    jp = _job_posting(text)
    if jp and isinstance(jp.get("validThrough"), str):
        if jp["validThrough"][:10] < today:
            return "closed", f"JobPosting validThrough {jp['validThrough'][:10]}"
        return "open", f"JobPosting valid through {jp['validThrough'][:10]}"
    main = " ".join(MAIN_RE.findall(text or "")) or (text or "")
    ok = bool(status and 200 <= status < 300)
    if ok and OPEN_RE.search(main):
        return "open", "apply control present"
    m = CLOSED_RE.search(main)
    if m:
        return "closed", f'page says "{m.group(0)}"'
    return "unknown", f"HTTP {status}" if status else "fetch failed"


def run(store, date: str, fetcher=fetch_status) -> Path:
    results = []
    for lead in store.list():
        if not eligible(lead):
            continue
        t = lead.get("triage") or {}
        url = t.get("jd_url") or lead.get("canonical_url")
        final, status, text = fetcher(url)
        v, ev = verdict(status, text)
        results.append({"lead_id": lead["lead_id"], "company": t.get("company"), "role_title": t.get("role_title"),
                        "url": url, "final_url": final, "verdict": v, "evidence": ev})
    results.sort(key=lambda r: (r["verdict"], str(r["company"] or "").lower()))
    out = paths.summaries_dir() / f"closed-check-{date}.md"
    out.with_suffix(".json").write_text(json.dumps({"date": date, "checked_at": datetime.now(timezone.utc).isoformat(),
                                                    "results": results}, ensure_ascii=False, indent=2), encoding="utf-8")
    lines = [f"# Closed-posting check — {date}", "",
             "Nothing has been removed. Confirm the Closed list (and decide each Unknown); removal then runs through "
             "the Jobs tab's Skip with reason `closed_stale`, so every lead keeps its `_skips.jsonl` record.", ""]
    for v, title in (("closed", "Closed"), ("unknown", "Unknown — needs a look"), ("open", "Open")):
        rows = [r for r in results if r["verdict"] == v]
        lines += [f"## {title} ({len(rows)})", ""] + [
            f"- `{r['lead_id']}` {r['company']} — {r['role_title']} · {r['evidence']} · {r['url']}" for r in rows] + [""]
    out.write_text("\n".join(lines), encoding="utf-8")
    return out


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("--date", default=datetime.now().strftime("%Y-%m-%d"))
    a = ap.parse_args(argv)
    print(f"wrote {run(LeadStore(), a.date)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
