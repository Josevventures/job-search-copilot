"""Duplicate-lead matcher (2026-10-29; docs/DESIGN-DECISIONS.md, "Duplicate leads").

The lead id is a hash of one canonical URL, so the same role dropped from two LinkedIn posts, or from LinkedIn and a
careers site, used to make two leads. This module compares a lead against every other known lead (board, applied, and
skipped via _skips.jsonl) and returns one of:

  confident -> fold it into the existing lead (no new lead on the board)
  fuzzy     -> keep it, but mark triage.possible_duplicate_of so the card offers "Skip as duplicate"
  None      -> different role

Conservative by design: a confident match needs the same specific posting URL, or the same company AND the same title
(word for word after normalization) with no location conflict. Same company with different titles is never confident
and is only fuzzy when the extra title words are noise (location, team, the company's own name). So "Payments
Product Associate" vs "...Product Manager", or three "Solutions Engineer, <segment>" roles at one company, stay
separate. Stdlib only, so the board can import it too.
"""
import re
from urllib.parse import urlparse

from lib.canonical import canonicalize

STOP = {"the", "a", "an", "of", "for", "and", "at", "in", "to", "with", "on", "by", "&"}
COMPANY_SUFFIXES = {"inc", "llc", "ltd", "lp", "llp", "co", "corp", "corporation", "company", "plc", "gmbh",
                    "careers", "jobs"}
# Words that may differ between two titles of the same role (where, which team, how it was posted). Seniority and
# function words are deliberately absent: "Senior", "Manager", "Engineer", "Real Estate" always mean another role.
TITLE_NOISE = {"team", "nyc", "ny", "sf", "la", "dc", "remote", "hybrid", "onsite", "office", "us", "usa", "based",
               "new", "york", "city", "san", "francisco", "los", "angeles", "position", "role", "opening", "hiring",
               "job", "full", "time", "fulltime", "permanent", "opportunity"}
# A listing page hosts many roles: sharing one proves nothing about the role.
GENERIC_TAILS = {"careers", "career", "jobs", "job", "work-with-us", "join-us", "join", "open-positions", "positions",
                 "opportunities", "openings", "careers.html", "jobs.html", "hiring", "team", "about"}
LINKEDIN_JOB_RE = re.compile(r"/jobs/view/(?:[^/]*?-)?(\d{6,})$")
_WORD_RE = re.compile(r"[a-z0-9]+")
_PAREN_RE = re.compile(r"\([^)]*\)")


def _words(text) -> list[str]:
    return _WORD_RE.findall(str(text or "").lower().replace("&", " and "))


def company_tokens(name) -> frozenset:
    """Every meaningful word of the company, parenthetical aliases included."""
    return frozenset(w for w in _words(name) if w not in STOP and w not in COMPANY_SUFFIXES)


def company_core(name) -> str:
    """The company without parentheticals or legal suffixes: 'Stonegate Health (Stonegate Ventures)' -> 'stonegate health'."""
    return " ".join(w for w in _words(_PAREN_RE.sub(" ", str(name or ""))) if w not in STOP and w not in COMPANY_SUFFIXES)


def company_relation(a, b) -> str | None:
    """'same' | 'related' (one name contains the other: 'Northwind' vs 'Northwind Ventures') | None."""
    ta, tb = company_tokens(a), company_tokens(b)
    if not ta or not tb:
        return None
    ca, cb = company_core(a), company_core(b)
    if (ca and ca == cb) or ta == tb:
        return "same"
    if ta <= tb or tb <= ta:
        return "related"
    return None


def title_key(title) -> str:
    """Normalized title, word for word: punctuation and case never matter."""
    return " ".join(w for w in _words(title) if w not in STOP)


def title_tokens(title) -> frozenset:
    return frozenset(w for w in _words(title) if w not in STOP)


def url_key(url) -> str | None:
    """A comparable key for a specific posting URL; None for empty or generic listing pages."""
    if not isinstance(url, str) or not url.strip().lower().startswith(("http://", "https://")):
        return None
    try:
        c = canonicalize(url)
    except Exception:  # noqa: BLE001 - a malformed URL just does not match
        return None
    p = urlparse(c)
    host = p.netloc.lower().removeprefix("www.")
    m = LINKEDIN_JOB_RE.search(p.path) if host.endswith("linkedin.com") else None
    if m:
        return f"linkedin-job:{m.group(1)}"
    if host.endswith("linkedin.com") and "currentjobid=" in p.query.lower():
        jid = re.search(r"currentjobid=(\d+)", p.query, re.I)
        if jid:
            return f"linkedin-job:{jid.group(1)}"
    tail = p.path.rstrip("/").rsplit("/", 1)[-1].lower()
    if not p.path.strip("/") or tail in GENERIC_TAILS:
        return None
    return f"{host}{p.path.rstrip('/')}".lower() + (f"?{p.query}" if p.query else "")


def _cities(locations) -> set[str]:
    out = set()
    for loc in locations or []:
        label = loc.get("label") if isinstance(loc, dict) else loc
        city = " ".join(_words(str(label or "").split(",")[0]))
        if city and not city.startswith(("remote", "multiple", "united states", "us ")):
            out.add(city)
    return out


def locations_conflict(a, b) -> bool:
    """True only when both leads name cities and none overlap (Los Angeles CA vs Los Angeles USA overlap)."""
    ca, cb = _cities(a), _cities(b)
    if not ca or not cb:
        return False
    return not any(x == y or x in y or y in x for x in ca for y in cb)


def _titles_similar(ta, tb, company_words) -> bool:
    """One title's words contain the other's and every extra word is noise or the company's own name."""
    a, b = title_tokens(ta), title_tokens(tb)
    if not a or not b:
        return False
    small, big = (a, b) if len(a) <= len(b) else (b, a)
    if not small <= big:
        return False
    return all(w in TITLE_NOISE or w in company_words or w.isdigit() for w in big - small)


def record(lead_or_skip: dict) -> dict:
    """A comparable record from a lead file or a _skips.jsonl line."""
    if "triage" in lead_or_skip or "intake" in lead_or_skip:
        t = lead_or_skip.get("triage") or {}
        d = lead_or_skip.get("decision") or {}
        i = lead_or_skip.get("intake") or {}
        urls = [t.get("jd_url"), lead_or_skip.get("canonical_url")] + list(i.get("jd_links") or [])
        state = "applied" if d.get("decision") == "applied" else "open"
        return {"lead_id": lead_or_skip.get("lead_id"), "company": t.get("company"), "role_title": t.get("role_title"),
                "urls": urls, "locations": t.get("locations") or ([t["location"]] if t.get("location") else []),
                "state": state, "created_at": i.get("created_at") or "", "skipped": False}
    return {"lead_id": lead_or_skip.get("lead_id"), "company": lead_or_skip.get("company"),
            "role_title": lead_or_skip.get("role_title"), "urls": [lead_or_skip.get("canonical_url")],
            "locations": [lead_or_skip["location"]] if lead_or_skip.get("location") else [],
            "state": "skipped", "created_at": lead_or_skip.get("skipped_at") or "", "skipped": True}


def compare(a: dict, b: dict) -> tuple[str, str] | None:
    """Two records (from record()) -> (kind, why) with kind 'confident' | 'fuzzy', or None."""
    ua = {k for k in map(url_key, a.get("urls") or []) if k}
    ub = {k for k in map(url_key, b.get("urls") or []) if k}
    shared = ua & ub
    rel = company_relation(a.get("company"), b.get("company"))
    ka, kb = title_key(a.get("role_title")), title_key(b.get("role_title"))
    if shared:
        # The same specific posting page. Only a clearly different title (a second role in one post) holds it back.
        if not ka or not kb or ka == kb or _titles_similar(a.get("role_title"), b.get("role_title"),
                                                           company_tokens(a.get("company")) | company_tokens(b.get("company"))):
            return "confident", "same posting URL"
        return "fuzzy", "same posting URL, different title"
    if not rel or not ka or not kb:
        return None
    words = company_tokens(a.get("company")) | company_tokens(b.get("company"))
    if ka == kb:
        if rel == "same" and not locations_conflict(a.get("locations"), b.get("locations")):
            return "confident", "same company and title"
        return "fuzzy", "same title" + (", related company name" if rel != "same" else ", different location")
    if _titles_similar(a.get("role_title"), b.get("role_title"), words):
        return "fuzzy", "same company, near-identical title"
    return None


def _preference(r: dict) -> tuple:
    """Which existing record a duplicate points at: an applied lead, then an open one, then a skip; oldest first."""
    return ({"applied": 0, "open": 1, "skipped": 2}.get(r.get("state"), 3), str(r.get("created_at") or ""),
            str(r.get("lead_id") or ""))


def best_match(new: dict, others: list[dict]) -> tuple[str, dict, str] | None:
    """(kind, other_record, why) for the strongest match of `new` among `others` (records), or None."""
    hits = []
    for o in others:
        if not o.get("lead_id") or o.get("lead_id") == new.get("lead_id"):
            continue
        r = compare(new, o)
        if r:
            hits.append((0 if r[0] == "confident" else 1, _preference(o), r[0], o, r[1]))
    if not hits:
        return None
    hits.sort(key=lambda h: (h[0], h[1]))
    _, _, kind, o, why = hits[0]
    return kind, o, why


def label(r: dict) -> str:
    return " — ".join(p for p in (r.get("company"), r.get("role_title")) if p) or str(r.get("lead_id"))


def report(leads: list[dict], skips: list[dict]) -> list[dict]:
    """Report-only scan: every pair the matcher flags, each pair once. Nothing is written."""
    recs = [record(l) for l in leads if isinstance(l, dict)] + [record(s) for s in skips if isinstance(s, dict)]
    recs = [r for r in recs if r.get("lead_id")]
    seen_ids, out = set(), []
    for i, a in enumerate(recs):
        if a["lead_id"] in seen_ids:
            continue
        seen_ids.add(a["lead_id"])
        for b in recs[i + 1:]:
            if b["lead_id"] == a["lead_id"]:
                continue
            r = compare(a, b)
            if r:
                out.append({"kind": r[0], "why": r[1], "a": a["lead_id"], "a_label": label(a), "a_state": a["state"],
                            "b": b["lead_id"], "b_label": label(b), "b_state": b["state"]})
    return out
