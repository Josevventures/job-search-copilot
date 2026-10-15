"""The adapter chain (addendum §3): Greenhouse API first, then the site DOM adapter, then the generic DOM adapter.
First adapter that finds fields (or a sign-in wall) wins; otherwise questions_visible=false with every error.
Every adapter runs inside `except Exception`: any failure is recorded in errors and the next adapter is tried."""
import time
from urllib.parse import urlsplit

from . import browser, dom, greenhouse, schema, sites
from .dom import _host


def _merge(result: dict, got: dict, adapter) -> dict:
    result["adapter"] = adapter.name
    if adapter.ats:
        result["ats"] = adapter.ats
    result["sign_in_required"] = bool(got["sign_in_required"])
    for n, fields in got["pages"]:
        schema.add_page(result, n, fields)
    for n, label, decision, reason in got["clicks"]:
        schema.add_click(result, n, label, decision, reason)
    result["unseen_pages"] = bool(got["unseen_pages"])
    for e in got["errors"]:
        schema.add_error(result, f"{adapter.name}: {e}")
    return schema.finalize(result)


def extract(url, timeout_s: int = 120, fetch_json=None, session=None, clock=time.monotonic) -> dict:
    fetch_json = fetch_json or greenhouse.http_get_json
    session = session or browser.open_session
    deadline = clock() + max(10, int(timeout_s))
    result = schema.new_result(url)
    parts = urlsplit(str(url or ""))
    if parts.scheme not in ("http", "https") or not parts.hostname:
        schema.add_error(result, "not an http(s) URL")
        return schema.finalize(result)
    result["ats"] = sites.ats_for(parts.hostname)
    gh = greenhouse.parse_url(url)
    if gh:
        try:
            data = fetch_json(greenhouse.api_url(*gh), timeout=min(30.0, max(5.0, deadline - clock())))
            return greenhouse.to_result(data, url)
        except Exception as e:
            schema.add_error(result, f"greenhouse_api: {type(e).__name__}: {e}")
    site = sites.match(url)
    for adapter in ([site] if site else []) + [sites.GENERIC]:
        target = adapter.apply_url(url)
        left = deadline - clock()
        if left < 5:
            schema.add_error(result, f"{adapter.name}: time limit reached before start")
            break
        try:
            # min(20, left) is the per-operation Playwright timeout, not a bound on the whole session
            with session({_host(url), _host(target)}, timeout_ms=int(min(20.0, left) * 1000)) as (page, guard):
                got = dom.crawl(page, target, guard, deadline=deadline, hint=adapter.hint, clock=clock)
            if any(fields for _, fields in got["pages"]) or got["sign_in_required"]:
                return _merge(result, got, adapter)
            for e in got["errors"]:
                schema.add_error(result, f"{adapter.name}: {e}")
            schema.add_error(result, f"{adapter.name}: no form fields found at {target}")
        except Exception as e:
            schema.add_error(result, f"{adapter.name}: {type(e).__name__}: {e}")
    return schema.finalize(result)
