"""Slack Web API, stdlib only: the three calls the Slack intake needs (history, replies, postMessage).

Auth: the SLACK_BOT_TOKEN environment variable (or a token passed in). The token is never logged or put in an
error message."""
from __future__ import annotations
import json, os, time, urllib.error, urllib.parse, urllib.request

API = "https://slack.com/api/"
_POST = {"chat.postMessage"}


class SlackError(Exception):
    def __init__(self, method: str, error: str):
        super().__init__(f"slack {method}: {error}")
        self.method, self.error = method, error


def _urlopen(req):
    try:
        with urllib.request.urlopen(req, timeout=20) as r:
            return r.status, dict(r.headers), r.read()
    except urllib.error.HTTPError as e:
        return e.code, dict(e.headers or {}), e.read()


def call(method: str, params: dict, *, token: str | None = None, http=None) -> dict:
    token = token or os.environ.get("SLACK_BOT_TOKEN")
    http = http or _urlopen
    for attempt in (1, 2):
        if method in _POST:
            req = urllib.request.Request(API + method, data=json.dumps(params).encode(), method="POST",
                                         headers={"Content-Type": "application/json; charset=utf-8"})
        else:
            req = urllib.request.Request(API + method + "?" + urllib.parse.urlencode(params), method="GET")
        if token:
            req.add_header("Authorization", f"Bearer {token}")
        status, headers, body = http(req)
        data = json.loads(body or b"{}")
        if data.get("ok"):
            return data
        err = data.get("error") or f"http_{status}"
        if err == "ratelimited" and attempt == 1:
            time.sleep(min(int(headers.get("Retry-After", "1")), 30))
            continue
        if err in ("not_authed", "invalid_auth") and not token:
            err += " (no SLACK_BOT_TOKEN in the environment or in .env; see docs/SETUP.md)"
        raise SlackError(method, err)
    raise SlackError(method, "ratelimited")


def post(channel: str, text: str, thread_ts: str | None = None, **kw) -> str:
    p = {"channel": channel, "text": text}
    if thread_ts:
        p["thread_ts"] = thread_ts
    return call("chat.postMessage", p, **kw)["ts"]


def replies(channel: str, ts: str, limit: int = 50, **kw) -> list[dict]:
    return call("conversations.replies", {"channel": channel, "ts": ts, "limit": limit}, **kw).get("messages", [])


def history(channel: str, oldest: str, limit: int = 20, **kw) -> list[dict]:
    return call("conversations.history", {"channel": channel, "oldest": oldest, "limit": limit}, **kw).get("messages", [])


def history_all(channel: str, oldest: str, **kw) -> list[dict]:
    """Every top-level message since `oldest`, following next_cursor (jobs-slack-intake reads a 14-day window)."""
    out, cursor = [], None
    while True:
        p = {"channel": channel, "oldest": oldest, "limit": 200}
        if cursor:
            p["cursor"] = cursor
        data = call("conversations.history", p, **kw)
        out += data.get("messages", [])
        cursor = (data.get("response_metadata") or {}).get("next_cursor")
        if not cursor:
            return out
