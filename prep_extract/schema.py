"""form_fields.json — the extractor's only output (addendum §3). Stdlib only: the board imports stub_result."""
import re
from datetime import datetime, timezone

from .sanitize import clean, parse_limits

SCHEMA_VERSION = 1
LABEL_CAP = 500
OPTION_CAP = 200
MAX_FIELDS = 200
MAX_OPTIONS = 100
MAX_CLICKS = 100
MAX_ERRORS = 20
ERROR_CAP = 300
URL_CAP = 2000
LIMIT_CAP = 1_000_000
DECISION_CAP = 50
FIELD_TYPES = ("text", "textarea", "select", "radio", "checkbox", "file", "email", "tel", "url", "number", "date")
_STAR_RE = re.compile(r"\s*[*\u2731\uff0a]\s*$")
# U+2731 HEAVY ASTERISK is only ever a required marker (Lever): unlike '*', it means "required" anywhere in a label.
_HEAVY_STAR = "\u2731"
_HEAVY_STAR_RE = re.compile(r"\s*\u2731\s*")
_COVER_RE = re.compile(r"cover\s*letter", re.I)
_RESUME_RE = re.compile(r"\b(?:r[e\u00e9]sum[e\u00e9]|cv)\b", re.I)


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def new_result(url) -> dict:
    return {"schema_version": SCHEMA_VERSION, "url": clean(url, URL_CAP) or "", "ats": None, "adapter": None,
            "extracted_at": _now(), "questions_visible": False, "sign_in_required": False, "pages": [],
            "cover_letter": {"mode": "none", "required": False}, "resume": {"present": False, "required": False},
            "click_log": [], "unseen_pages": False, "errors": []}


def add_error(result: dict, msg) -> None:
    if len(result["errors"]) < MAX_ERRORS:
        result["errors"].append(clean(msg, ERROR_CAP) or "error")


def stub_result(url, error) -> dict:
    """What the board writes when the extractor crashes, times out or produces junk."""
    r = new_result(url)
    add_error(r, error)
    return r


def _page_int(value) -> int:
    try:
        return int(value)
    except (TypeError, ValueError, OverflowError):
        return 0


def field_count(result: dict) -> int:
    return sum(len(p["fields"]) for p in result["pages"])


def make_field(raw: dict) -> dict:
    label = clean(raw.get("label"), LABEL_CAP) or ""
    starred = bool(_STAR_RE.search(label)) or _HEAVY_STAR in label
    label = _HEAVY_STAR_RE.sub(" ", _STAR_RE.sub("", label)).strip()
    help_text = clean(raw.get("help_text"), LABEL_CAP) or None
    ftype = raw.get("type") if raw.get("type") in FIELD_TYPES else "text"
    text_chars, text_words = parse_limits(f"{label} {help_text or ''}")
    ml = raw.get("maxlength")
    max_chars = ml if isinstance(ml, int) and not isinstance(ml, bool) and ml > 0 else text_chars
    max_chars = min(max_chars, LIMIT_CAP) if max_chars is not None else None
    text_words = min(text_words, LIMIT_CAP) if text_words is not None else None
    opts = raw.get("options") if isinstance(raw.get("options"), list) else []
    options = [o for o in (clean(x, OPTION_CAP) for x in opts[:MAX_OPTIONS]) if o]
    field = {"label": label, "type": ftype, "required": bool(raw.get("required")) or starred,
             "max_chars": max_chars, "max_words": text_words, "options": options, "help_text": help_text}
    if not label:
        field["label_missing"] = True  # the page gave this control no readable question; the drafter flags it
    return field


def add_page(result: dict, page_no: int, raw_fields: list) -> None:
    room = MAX_FIELDS - field_count(result)
    fields = []
    for raw in raw_fields or []:
        if not isinstance(raw, dict):
            continue
        if len(fields) >= room:
            add_error(result, f"field limit reached ({MAX_FIELDS}); remaining fields dropped")
            break
        fields.append(make_field(raw))
    result["pages"].append({"page": _page_int(page_no), "fields": fields})


def add_click(result: dict, page_no, label, decision, reason) -> None:
    if len(result["click_log"]) < MAX_CLICKS:
        result["click_log"].append({"page": _page_int(page_no), "label": clean(label, OPTION_CAP) or "",
                                    "decision": clean(decision, DECISION_CAP) or "", "reason": clean(reason, OPTION_CAP) or ""})


def finalize(result: dict) -> dict:
    """Derive cover_letter / resume / questions_visible from the fields. Call last."""
    cover = {"mode": "none", "required": False}
    resume = {"present": False, "required": False}
    for page in result["pages"]:
        for f in page["fields"]:
            if _COVER_RE.search(f["label"]):
                if f["type"] == "file":
                    cover["mode"] = "upload"
                elif f["type"] in ("textarea", "text") and cover["mode"] != "upload":
                    cover["mode"] = "text"
                cover["required"] = cover["required"] or f["required"]
            elif f["type"] == "file" and _RESUME_RE.search(f["label"]):
                resume = {"present": True, "required": resume["required"] or f["required"]}
    result["cover_letter"], result["resume"] = cover, resume
    result["questions_visible"] = (not result["sign_in_required"]) and field_count(result) > 0
    return result
