"""Thin requests wrapper. Never raises: returns (url, "") on any failure."""
import requests

HEADERS = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/128 Safari/537.36",
           "Accept-Language": "en-US"}


def fetch(url: str, timeout: int = 25) -> tuple[str, str]:
    try:
        r = requests.get(url, headers=HEADERS, timeout=timeout, allow_redirects=True)
        return r.url, r.text
    except Exception:
        return url, ""


def fetch_status(url: str, timeout: int = 25) -> tuple[str, int | None, str]:
    """Like fetch() but keeps the HTTP status (closed check). Never raises."""
    try:
        r = requests.get(url, headers=HEADERS, timeout=timeout, allow_redirects=True)
        return r.url, r.status_code, r.text
    except Exception:
        return url, None, ""
