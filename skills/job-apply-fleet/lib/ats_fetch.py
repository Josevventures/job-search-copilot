"""Scoped, read-only ATS fetch for the scheduled Slack intake (2026-10-14: the headless triage run could not read
Workday postings, so the Python ingest layer reads them first).

Workday career pages are JS-only, so the job description comes from Workday's public cxs JSON API. Scope, enforced
here and not by convention: https only, hosts ending in `.myworkdayjobs.com` only (redirects included), GET only,
15 s timeout, 2 MB response cap, no cookies, no auth. Never raises: any failure returns None (and is logged).
"""
import html as htmllib
import json
import re
import time
import urllib.error
import urllib.request
from html.parser import HTMLParser
from urllib.parse import urlsplit

WORKDAY_SUFFIX = ".myworkdayjobs.com"
TIMEOUT_S = 15
MAX_BYTES = 2 * 1024 * 1024
CHUNK = 64 * 1024
_HOSTNAME = re.compile(r"^[a-z0-9-]+(\.[a-z0-9-]+)*\.myworkdayjobs\.com$")
ERR_MAX = 200
UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/128 Safari/537.36"
_LOCALE = re.compile(r"^[a-z]{2}(-[A-Za-z]{2,4})?$")


def _host(url):
    try:
        p = urlsplit(url)
        return p.scheme, (p.hostname or "").lower(), p.netloc
    except (TypeError, ValueError, AttributeError):
        return None, None, None


def is_workday(url) -> bool:
    """https, and a clean hostname (labels of [a-z0-9-] only) ending in .myworkdayjobs.com."""
    scheme, host, netloc = _host(url)
    return scheme == "https" and bool(host) and "\\" not in (netloc or "") and bool(_HOSTNAME.match(host))


def cxs_url(url):
    """https://<tenant>.<wdN>.myworkdayjobs.com/<locale?>/<site>/job/<rest> -> .../wday/cxs/<tenant>/<site>/job/<rest>.
    None for anything else."""
    if not isinstance(url, str) or not is_workday(url):
        return None
    p = urlsplit(url)
    host = p.hostname.lower()
    tenant = host[:-len(WORKDAY_SUFFIX)].split(".")[0]
    parts = [x for x in p.path.split("/") if x]
    if parts and _LOCALE.match(parts[0]):
        parts = parts[1:]
    if len(parts) < 3 or parts[1] != "job" or not tenant:
        return None
    site, rest = parts[0], "/".join(parts[2:])
    return f"https://{host}/wday/cxs/{tenant}/{site}/job/{rest}"


class _Text(HTMLParser):
    BLOCK = {"p", "br", "li", "ul", "ol", "div", "h1", "h2", "h3", "h4", "h5", "h6", "tr"}

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.out = []

    def handle_starttag(self, tag, attrs):
        if tag in self.BLOCK:
            self.out.append("\n")
        if tag == "li":
            self.out.append("- ")

    def handle_endtag(self, tag):
        if tag in self.BLOCK:
            self.out.append("\n")

    def handle_data(self, data):
        self.out.append(data)


def html_to_text(s: str) -> str:
    p = _Text()
    p.feed(s or "")
    p.close()
    lines = [" ".join(htmllib.unescape(ln).split()) for ln in "".join(p.out).splitlines()]
    return "\n".join(ln for ln in lines if ln)


class _SameHostRedirect(urllib.request.HTTPRedirectHandler):
    """Follow a redirect only to another https Workday URL."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        if not is_workday(newurl):
            raise urllib.error.HTTPError(newurl, code, "redirect off myworkdayjobs.com refused", headers, fp)
        return super().redirect_request(req, fp, code, msg, headers, newurl)


def _default_opener():
    # build_opener adds no cookie handler; the redirect handler replaces the default one.
    return urllib.request.build_opener(_SameHostRedirect)


def fetch_workday_jd(url, *, opener=None, log=None):
    """Plain text (title, location, description) of a Workday posting, or None. Never raises."""
    api = cxs_url(url)
    if api is None:
        return None
    try:
        req = urllib.request.Request(api, method="GET", headers={"Accept": "application/json", "User-Agent": UA})
        deadline = time.monotonic() + TIMEOUT_S   # a total deadline: the socket timeout is per read
        chunks, size = [], 0
        with (opener or _default_opener()).open(req, timeout=TIMEOUT_S) as r:
            while True:
                if time.monotonic() > deadline:
                    raise TimeoutError(f"no complete response within {TIMEOUT_S} s")
                # read1 does at most one socket read, so a slow drip can't hold one call past the deadline
                chunk = r.read1(CHUNK) if hasattr(r, "read1") else r.read(CHUNK)
                if not chunk:
                    break
                size += len(chunk)
                if size > MAX_BYTES:
                    raise ValueError("response over 2 MB")
                chunks.append(chunk)
        body = b"".join(chunks)
        info = json.loads(body.decode("utf-8", errors="replace")).get("jobPostingInfo") or {}
        desc = html_to_text(info.get("jobDescription") or "")
        if not desc:
            raise ValueError("no jobDescription in jobPostingInfo")
        head = [x.strip() for x in (info.get("title"), info.get("location")) if isinstance(x, str) and x.strip()]
        return "\n".join(head + ["", desc]) + "\n"
    except Exception as e:  # noqa: BLE001 - a prefetch failure only means triage falls back to its own ladder
        if log:
            log(f"workday fetch failed {api}: {type(e).__name__}: {e}"[:ERR_MAX])
        return None
