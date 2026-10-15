"""Greenhouse adapter: the public board API returns the application questions as JSON (addendum §3.1).
GET only, no redirects, no browser."""
import html
import re

from . import schema

UA = "Mozilla/5.0 (compatible; prep-extract/1.0; read-only)"
API = "https://boards-api.greenhouse.io/v1/boards/{board}/jobs/{job}?questions=true"
MAX_BYTES = 5_000_000
_PATTERNS = (
    re.compile(r"^https?://boards-api\.greenhouse\.io/v1/boards/(?P<board>[\w-]+)/jobs/(?P<job>\d+)(?:[/?#].*)?$", re.I),
    re.compile(r"^https?://(?:job-boards|boards)\.greenhouse\.io/(?P<board>[\w-]+)/jobs/(?P<job>\d+)(?:[/?#].*)?$", re.I),
    re.compile(r"^https?://(?:job-boards|boards)\.greenhouse\.io/embed/job_app\?"
               r"(?=(?:.*&)?for=(?P<board>[\w-]+))(?=(?:.*&)?token=(?P<job>\d+)).*$", re.I),
)
_TYPE_MAP = {"input_text": "text", "textarea": "textarea", "multi_value_single_select": "select",
             "multi_value_multi_select": "checkbox", "input_file": "file"}


def parse_url(url) -> tuple[str, str] | None:
    for p in _PATTERNS:
        m = p.match(str(url or ""))
        if m:
            return m.group("board"), m.group("job")
    return None


def api_url(board: str, job: str) -> str:
    return API.format(board=board, job=job)


def http_get_json(url: str, timeout: float = 30.0) -> dict:
    import requests  # lazy: the board's Python imports this package's stdlib modules only
    r = requests.get(url, timeout=timeout, allow_redirects=False,
                     headers={"User-Agent": UA, "Accept": "application/json"})
    if r.status_code != 200:
        raise RuntimeError(f"HTTP {r.status_code}")
    if len(r.content) > MAX_BYTES:
        raise RuntimeError("response too large")
    return r.json()


def _text(s) -> str:
    return re.sub(r"\s+", " ", html.unescape(re.sub(r"<[^>]+>", " ", str(s or "")))).strip()


def _field(q: dict) -> dict | None:
    fs = [f for f in (q.get("fields") or []) if isinstance(f, dict) and f.get("type") != "input_hidden"]
    if not fs:
        return None
    types = [f.get("type") for f in fs]
    ftype = "file" if "input_file" in types else _TYPE_MAP.get(types[0], "text")
    options = [v.get("label") for f in fs for v in (f.get("values") or []) if isinstance(v, dict) and v.get("label")]
    help_text = _text(q.get("description"))
    if ftype == "file" and "textarea" in types:
        help_text = f"{help_text} (or paste as text)".strip()
    return {"label": q.get("label"), "type": ftype, "required": bool(q.get("required")),
            "options": options, "help_text": help_text}


def to_result(data, url: str) -> dict:
    if not isinstance(data, dict) or not isinstance(data.get("questions"), list):
        raise ValueError("no questions in the API response")
    result = schema.new_result(url)
    result["ats"], result["adapter"] = "greenhouse", "greenhouse_api"
    questions = [q for q in data["questions"] + list(data.get("location_questions") or []) if isinstance(q, dict)]
    schema.add_page(result, 1, [f for f in (_field(q) for q in questions) if f])
    return schema.finalize(result)
