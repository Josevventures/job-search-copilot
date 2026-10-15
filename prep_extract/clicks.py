"""Navigation-only click rules (addendum §2.3, §3.3). Pure: decide() looks at one control described by
dom.CONTROLS_JS. A control is clickable only if it is a button/link/tab, not a submit control, its normalized label
matches ALLOW_RE (or it is an unselected step tab) and matches no deny word. Deny always wins."""
import re
from urllib.parse import urlsplit

from .sanitize import norm_label

ALLOW_RE = re.compile(r"\b(?:next|continue|save (?:and|&) continue|next step|step \d+)\b")
# The addendum's list, verbatim. Deliberately unanchored (no leading \b): "resubmit", "reapply", "submitting",
# "reviewed" are denied too. The cost is an occasional false deny, which only means one fewer page read.
DENY_RE = re.compile(r"(?:submit|apply|send|finish|review|sign ?in|log ?in|create|register|accept|agree|upload|"
                     r"attach|pay|confirm|complete|done|authori[sz]e|allow|grant)")
# Added (plan decision): third-party sign-in buttons such as "Continue with LinkedIn" are never navigation.
SSO_RE = re.compile(r"\b(?:linkedin|google|indeed|microsoft|apple|facebook|github|sso|oauth)\b")
SUBMIT_TYPES = ("submit", "image", "reset")


def _link_problem(href, target, page_host: str) -> str | None:
    if not href:
        return "link without an href"
    if "\\" in href:
        return "suspicious URL"
    parts = urlsplit(href)
    if parts.scheme not in ("http", "https"):
        return f"link scheme {parts.scheme or 'none'}"
    if "@" in parts.netloc:
        return "suspicious URL"
    if (parts.hostname or "").lower() != (page_host or "").lower():
        return "link leaves the posting host"
    if str(target or "").lower() not in ("", "_self"):
        return "link opens a new window"
    return None


def decide(c: dict, page_host: str) -> tuple[str, str] | None:
    label = norm_label(c.get("label"))
    role = str(c.get("role") or "")
    ctype = str(c.get("type") or "").lower()
    step_tab = role == "tab" and bool(c.get("tab_in_list"))
    if step_tab and c.get("selected"):
        return None  # the step we are already on
    # Every name the control carries (aria-label, text, value, title, img alt, ::before/::after text...). The
    # primary label says what the control claims to be; any of them can veto it.
    texts = [label] + [norm_label(t) for t in c.get("labels") or []]
    texts = [t for t in texts if t]
    allow_word = ALLOW_RE.search(label)
    deny_word = next((m for t in texts if (m := DENY_RE.search(t) or SSO_RE.search(t))), None)
    odd = any(not t.isascii() for t in texts)
    bidi = bool(c.get("bidi"))
    relevant = allow_word or deny_word or step_tab or ctype in SUBMIT_TYPES or odd or bidi
    if not relevant:
        return None
    if bidi:
        return ("deny", "label has bidirectional or format control characters")
    if not label:
        return ("deny", "no accessible label")
    if odd:
        return ("deny", "label has non-ASCII characters (possible look-alike)")
    if deny_word:
        return ("deny", f"label contains '{deny_word.group(0)}'")
    if role not in ("button", "link", "tab"):
        return ("deny", f"role {role or 'none'} is not button/link/tab")
    if ctype in SUBMIT_TYPES:
        return ("deny", f"type={ctype}")
    if c.get("form_attrs"):
        return ("deny", "carries form-submission attributes")
    if c.get("disabled"):
        return ("deny", "disabled")
    if c.get("tag") == "a" or role == "link":
        problem = _link_problem(c.get("href"), c.get("target"), page_host)
        if problem:
            return ("deny", problem)
    return ("allow", "step tab" if step_tab else "navigation control")
