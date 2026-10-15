"""Parse LinkedIn guest HTML for a post: text, outbound links, poster, roundup detection."""
import html as htmllib
import re
from dataclasses import dataclass, field
from urllib.parse import urlparse

from lib.canonical import is_shortlink, unwrap_static

ATS_HOST_PARTS = ("ashbyhq.com", "greenhouse.io", "lever.co", "myworkdayjobs.com", "workable.com", "icims.com",
                  "ultipro.com", "smartrecruiters.com", "pinpointhq.com", "jobright.ai", "rippling.com",
                  "bamboohr.com", "jobvite.com", "recruitee.com", "breezy.hr", "typeform.com", "docs.google.com")
JOB_PATH_RE = re.compile(r"/(jobs?|careers?|apply|positions?|openings?|opportunit(y|ies)|fellowship)(/|$|\?|-)", re.I)
LINKEDIN_NON_JOB = ("/company/", "/showcase/", "/feed/hashtag/", "/signup/", "/in/", "/school/", "/legal/", "/help/")
ROUNDUP_MIN = 3


@dataclass
class Post:
    title: str = ""
    description: str = ""
    body: str = ""
    links: list = field(default_factory=list)
    poster_name: str | None = None
    login_walled: bool = False


def _meta(html: str, prop: str) -> str:
    m = re.search(r'<meta[^>]+(?:property|name)="%s"[^>]+content="([^"]*)"' % re.escape(prop), html)
    return htmllib.unescape(m.group(1)) if m else ""


def parse_post(html: str) -> Post:
    title = _meta(html, "og:title")
    if title.strip().lower().startswith("sign up") or "signup/cold-join" in html[:2000]:
        return Post(title=title, login_walled=True)
    m = re.search(r'<p[^>]*attributed-text-segment-list__content[^>]*>(.*?)</p>', html, re.S)
    raw = m.group(1) if m else ""
    raw = re.sub(r"<br\s*/?>", "\n", raw)
    links, body_parts, last = [], [], 0
    for a in re.finditer(r'<a[^>]+href="([^"]+)"[^>]*>(.*?)</a>', raw, re.S):
        before = htmllib.unescape(re.sub(r"<[^>]+>", "", raw[last:a.start()]))
        body_parts.append(before)
        url = unwrap_static(htmllib.unescape(a.group(1)))
        links.append((url, before.strip()[-160:]))
        body_parts.append(htmllib.unescape(re.sub(r"<[^>]+>", "", a.group(2))))
        last = a.end()
    body_parts.append(htmllib.unescape(re.sub(r"<[^>]+>", "", raw[last:])))
    poster = None
    if "|" in title:
        # og:title shapes: "Text | Poster", "Text | Poster | 23 comments", "Role | Example Careers | Poster",
        # "Text | Poster posted on the topic | LinkedIn". The poster is the last meaningful segment.
        parts = [re.sub(r"\s+posted on the topic$", "", x.strip()) for x in title.split("|")[1:]]
        parts = [x for x in parts if x and x.lower() != "linkedin" and not re.fullmatch(r"\d+\s+comments?", x.lower())]
        poster = parts[-1] if parts else None
    return Post(title=title, description=_meta(html, "og:description"), body="".join(body_parts).strip(),
                links=links, poster_name=poster)


def is_job_link(url: str) -> bool:
    p = urlparse(url)
    host, path = p.netloc.lower(), p.path
    if host.endswith("linkedin.com"):
        return path.startswith("/jobs/view/")
    if host in {"youtu.be", "youtube.com", "www.youtube.com"}:
        return False
    if any(part in host for part in ATS_HOST_PARTS):
        return True
    return bool(JOB_PATH_RE.search(path))


def candidate_links(post: Post) -> list[tuple[str, str]]:
    seen, out = set(), []
    for url, hint in post.links:
        p = urlparse(url)
        if p.netloc.lower().endswith("linkedin.com") and any(x in p.path for x in LINKEDIN_NON_JOB):
            continue
        if not (is_job_link(url) or is_shortlink(url)):
            continue
        if url in seen:
            continue
        seen.add(url)
        out.append((url, hint))
    return out


def is_roundup(post: Post) -> bool:
    return len(candidate_links(post)) >= ROUNDUP_MIN
