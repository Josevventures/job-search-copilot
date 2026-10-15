"""Stage 1 of /job-apply-fleet: turn raw links into lead files (intake group only). No LLM."""
import argparse
import html as htmllib
import json
import re
import sys
from pathlib import Path
from urllib.parse import urlparse

sys.path.insert(0, str(Path(__file__).resolve().parent))

from lib import fetch as fetchmod  # noqa: E402
from lib.canonical import canonicalize, is_linkedin_post, is_shortlink, is_x_post, lead_id as make_lead_id, unwrap_static  # noqa: E402
from lib.lead_store import LeadStore, LeadStoreError  # noqa: E402
from lib.post_parser import ROUNDUP_MIN, candidate_links, is_job_link, parse_post  # noqa: E402

SCREEN_LABELS = {"job", "event", "non_job", "internship_or_undergrad"}


def read_urls(xlsx, file, args):
    raw = []
    if xlsx:
        import openpyxl
        wb = openpyxl.load_workbook(xlsx)
        for ws in wb.worksheets:
            for row in ws.iter_rows():
                for c in row:
                    v = c.hyperlink.target if c.hyperlink else c.value
                    if v is not None:
                        raw.append(str(v))
    if file:
        raw += Path(file).read_text(encoding="utf-8").splitlines()
    raw += list(args or [])
    urls, ignored = [], 0
    for v in raw:
        v = v.strip()
        if not v:
            continue
        if v.lower().startswith(("http://", "https://")):
            urls.append(v)
        else:
            ignored += 1
    return urls, ignored


def resolve(url, fetcher):
    url = unwrap_static(url)
    if is_shortlink(url):
        final, text = fetcher(url)
        if is_shortlink(final) and text:
            m = re.search(r'<a[^>]+href="(https?://(?!lnkd\.in|www\.linkedin\.com/(?:legal|help|signup|login))[^"]+)"', text)
            if m:
                final = htmllib.unescape(m.group(1))
        url = unwrap_static(final)
    return url


def _og(text):
    def g(prop):
        m = re.search(r'<meta[^>]+(?:property|name)="%s"[^>]+content="([^"]*)"' % prop, text or "")
        return htmllib.unescape(m.group(1)) if m else ""
    return {"title": g("og:title")[:300], "description": g("og:description")[:600]}


def _source(url, kind, via, poster=None, roundup_of=None, hint=None, note=None, slack=None):
    src = {"url": url, "kind": kind, "poster_name": poster, "poster_url": url if kind == "post" else roundup_of,
           "added_via": via, "roundup_of": roundup_of, "hint": hint}
    if note:
        src["note"] = note
    if slack:
        src["slack"] = slack
    return src


_SKIPPED = object()


def _err(e):
    return f"{type(e).__name__}: {e}"[:200]


def ingest(urls, via, store, fetcher=fetchmod.fetch, note=None, slack=None):
    """One bad URL never stops the run: each URL (and each roundup child) is isolated, recorded in
    rep["failed"], and rep["by_url"] maps every raw URL to its outcome (spec 2026-10-14 jobs-slack-intake §2)."""
    rep = {"created": 0, "merged": 0, "roundups": 0, "login_walled": 0, "ignored": 0,
           "previously_skipped": 0, "lead_ids": [], "failed": [], "by_url": {}}
    skipped = store.skipped_ids()  # Jobs-tab skip log: never re-create (and re-pay triage for) a rejected job
    extra = {"note": note or None, "slack": slack or None}

    def put(canonical, source, **fields):
        if make_lead_id(canonical) in skipped:
            rep["previously_skipped"] += 1
            return _SKIPPED
        lid, created = store.create_or_merge(canonical, source, **fields)
        rep["created" if created else "merged"] += 1
        if lid not in rep["lead_ids"]:
            rep["lead_ids"].append(lid)
        return lid

    for raw in urls:
        entry = rep["by_url"].setdefault(raw, {"status": "ok", "lead_ids": [], "roundup": False})
        try:
            _ingest_one(raw, via, fetcher, put, rep, entry, extra)
        except Exception as e:  # noqa: BLE001 - isolation is the point; the error is recorded, not swallowed
            entry["status"], entry["error"] = "failed", _err(e)
            rep["failed"].append({"url": raw, "error": entry["error"]})
        skipped_any = entry.pop("skipped", False)
        if entry["status"] in ("ok", "login_walled") and not entry["lead_ids"]:
            if skipped_any:
                entry["status"] = "previously_skipped"
            else:
                entry["status"], entry["error"] = "failed", "no lead created (every link in the post failed)"
                rep["failed"].append({"url": raw, "error": entry["error"]})
    return rep


def _ingest_one(raw, via, fetcher, put, rep, entry, extra):
    def add(r):
        if r is _SKIPPED:
            entry["skipped"] = True
        elif r and r not in entry["lead_ids"]:
            entry["lead_ids"].append(r)

    def child(u, fn):  # a failing roundup child or candidate link is recorded; its siblings continue
        try:
            return fn()
        except Exception as e:  # noqa: BLE001
            rep["failed"].append({"url": u, "error": _err(e)})
            return None

    url = resolve(raw, fetcher)
    if is_linkedin_post(url):
        final, text = fetcher(url)
        post = parse_post(text) if text else None
        post_url = canonicalize(url)
        if post is None or post.login_walled:
            rep["login_walled"] += 1
            entry["status"] = "login_walled"
            add(put(post_url, _source(post_url, "post", via, **extra), jd_links=[],
                    post_text="" if post is None else "LOGIN_WALLED"))
            return
        cands = []
        for u, h in candidate_links(post):
            r = child(u, lambda u=u: resolve(u, fetcher))
            if r:
                cands.append((r, h))
        seen, jobs = set(), []
        for u, h in cands:  # roundup = >= ROUNDUP_MIN distinct links that resolve to job-looking URLs
            if is_job_link(u) and u not in seen:
                seen.add(u)
                jobs.append((u, h))
        if len(jobs) >= ROUNDUP_MIN:
            rep["roundups"] += 1
            entry["roundup"] = True
            for u, h in jobs:
                add(child(u, lambda u=u, h=h: put(
                    canonicalize(u), _source(u, "posting", via, poster=post.poster_name, roundup_of=post_url,
                                             hint=h[-120:] or None, **extra), jd_links=[u])))
            return
        links = [u for u, _ in jobs]
        canonical = canonicalize(links[0]) if len(links) == 1 else post_url
        fields = {}
        if post.title or post.description:
            fields["preview"] = {"title": post.title[:300], "description": post.description[:600]}
        add(put(canonical, _source(post_url, "post", via, poster=post.poster_name, **extra),
                jd_links=links, post_text=post.body[:2000], **fields))
    elif is_x_post(url):
        add(put(canonicalize(url), _source(url, "x", via, **extra), jd_links=[]))
    else:
        kind = "form" if "docs.google.com/forms" in url or "typeform.com" in url else "posting"
        _, text = fetcher(url)
        add(put(canonicalize(url), _source(url, kind, via, **extra), jd_links=[url], preview=_og(text)))


def export_screen(store, out):
    items = []
    for lead in store.list():
        intake = lead.get("intake", {})
        if intake.get("screen") or (lead.get("triage") or {}).get("triage_status") == "complete":
            continue
        prev = intake.get("preview") or {}
        post_text = intake.get("post_text") or ""
        if post_text == "LOGIN_WALLED":
            post_text = ""
        items.append({"lead_id": lead["lead_id"], "url": lead["canonical_url"],
                      "host": urlparse(lead["canonical_url"]).netloc,
                      "title": prev.get("title", ""),
                      "text": (prev.get("description") or post_text)[:500]})
    Path(out).write_text(json.dumps(items, ensure_ascii=False, indent=2), encoding="utf-8")
    return len(items)


def apply_screen(store, path):
    applied = 0
    for row in json.loads(Path(path).read_text(encoding="utf-8")):
        try:
            if row.get("label") not in SCREEN_LABELS:
                print(f"WARN ignoring unknown label for {row.get('lead_id')}: {row.get('label')!r}", file=sys.stderr)
                continue
            store.update(row["lead_id"], "intake", {"screen": {"label": row["label"], "reason": row.get("reason", "")}})
            applied += 1
        except (KeyError, TypeError, AttributeError, LeadStoreError) as e:
            print(f"WARN skipping bad screen row {row!r}: {e!r}", file=sys.stderr)
    return applied


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("urls", nargs="*")
    ap.add_argument("--xlsx")
    ap.add_argument("--file")
    ap.add_argument("--via", choices=["sheet", "slack", "manual"], default="manual")
    ap.add_argument("--export-screen")
    ap.add_argument("--apply-screen")
    a = ap.parse_args(argv)
    store = LeadStore()
    if a.export_screen:
        print(f"exported {export_screen(store, a.export_screen)} unscreened leads -> {a.export_screen}")
        return 0
    if a.apply_screen:
        print(f"applied {apply_screen(store, a.apply_screen)} screen labels")
        return 0
    urls, ignored = read_urls(a.xlsx, a.file, a.urls)
    rep = ingest(urls, a.via, store)
    rep["ignored"] = ignored
    out = {k: v for k, v in rep.items() if k not in ("lead_ids", "by_url", "failed")}
    out["failed"] = len(rep.get("failed", []))
    print(json.dumps(out))
    print(f"lead_ids: {len(rep['lead_ids'])}")
    for f in rep.get("failed", []):
        print(f"FAILED {f['url']}: {f['error']}", file=sys.stderr)
    by_url = rep.get("by_url") or {}
    return 4 if by_url and all(e["status"] == "failed" for e in by_url.values()) else 0


if __name__ == "__main__":
    sys.exit(main())
