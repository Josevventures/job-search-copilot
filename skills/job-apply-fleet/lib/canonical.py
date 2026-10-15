"""Unwrap LinkedIn/redirect wrappers and canonicalize job URLs into stable lead ids."""
import hashlib
from urllib.parse import parse_qs, parse_qsl, urlencode, urlparse, urlunparse

TRACKING_KEYS = {"trk", "trackingid", "rcm", "fbclid", "gclid", "refid", "mt", "urlhash", "issdui",
                 "lipi", "midtoken", "midsig", "eid", "otptoken"}
TRACKING_SOURCE_VALUES = {"share", "linkedin", "linkedin_web", "li", "twitter", "x"}
X_HOSTS = {"x.com", "twitter.com", "www.x.com", "www.twitter.com", "mobile.twitter.com"}


def _q(url: str, key: str):
    vals = parse_qs(urlparse(url).query).get(key)
    return vals[0] if vals else None


def unwrap_static(url: str) -> str:
    """Resolve wrappers that carry the destination in the URL itself (no network)."""
    url = url.strip()
    for _ in range(4):
        p = urlparse(url)
        host, path = p.netloc.lower(), p.path
        if host.endswith("linkedin.com") and path.startswith("/safety/go") and _q(url, "url"):
            url = _q(url, "url")
        elif host.endswith("linkedin.com") and path.startswith("/redir/redirect") and _q(url, "url"):
            url = _q(url, "url")
        elif host.endswith("linkedin.com") and path.startswith("/signup/cold-join") and _q(url, "session_redirect"):
            url = _q(url, "session_redirect")
        else:
            return url
    return url


def canonicalize(url: str) -> str:
    url = unwrap_static(url.strip())
    p = urlparse(url)
    host = p.netloc.lower()
    keep = []
    for k, v in parse_qsl(p.query, keep_blank_values=True):
        kl = k.lower()
        if kl.startswith("utm_") or kl in TRACKING_KEYS:
            continue
        if kl == "source" and v.lower() in TRACKING_SOURCE_VALUES:
            continue
        if host in X_HOSTS and kl in {"s", "t"}:
            continue
        keep.append((k, v))
    path = p.path.rstrip("/") or ""
    return urlunparse((p.scheme.lower() or "https", host, path, "", urlencode(keep), ""))


def lead_id(canonical_url: str) -> str:
    return hashlib.sha256(canonical_url.strip().lower().encode("utf-8")).hexdigest()[:12]


def is_shortlink(url: str) -> bool:
    return urlparse(url).netloc.lower() in {"lnkd.in", "www.lnkd.in", "bit.ly", "t.co"}


def is_linkedin_post(url: str) -> bool:
    p = urlparse(url)
    return p.netloc.lower().endswith("linkedin.com") and (p.path.startswith("/posts/") or p.path.startswith("/feed/update/"))


def is_x_post(url: str) -> bool:
    p = urlparse(url)
    return p.netloc.lower() in X_HOSTS and "/status/" in p.path
