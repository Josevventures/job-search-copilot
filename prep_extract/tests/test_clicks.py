import pytest

from prep_extract import clicks

HOST = "jobs.example.com"
ZW = "Sub​mit"
CYR = "Nеxt"


def ctl(label, **over):
    base = {"idx": 0, "tag": "button", "role": "button", "type": "button", "label": label, "href": None,
            "target": "", "form_attrs": False, "tab_in_list": False, "selected": False, "disabled": False}
    base.update(over)
    return base


LINK = {"tag": "a", "role": "link", "type": ""}


@pytest.mark.parametrize("label,over,decision", [
    ("Next", {}, "allow"),
    ("NEXT", {}, "allow"),
    ("Continue", {}, "allow"),
    ("Save and continue", {}, "allow"),
    ("Save & Continue", {}, "allow"),
    ("Next step", {}, "allow"),
    ("Step 3", {}, "allow"),
    ("Continue application", {}, "allow"),
    ("Submit", {}, "deny"),
    ("Submit application", {}, "deny"),
    ("Apply now", {}, "deny"),
    ("Send", {}, "deny"),
    ("Finish", {}, "deny"),
    ("Review and submit", {}, "deny"),
    ("Review", {}, "deny"),
    ("Next: Review", {}, "deny"),
    ("Continue to apply", {}, "deny"),
    ("Resubmit and continue", {}, "deny"),
    ("Reapply", {}, "deny"),
    ("Sign in", {}, "deny"),
    ("Signin", {}, "deny"),
    ("Log in", {}, "deny"),
    ("Create account", {}, "deny"),
    ("Register", {}, "deny"),
    ("I agree", {}, "deny"),
    ("Accept", {}, "deny"),
    ("Upload resume", {}, "deny"),
    ("Attach", {}, "deny"),
    ("Pay", {}, "deny"),
    ("Confirm", {}, "deny"),
    ("Complete", {}, "deny"),
    ("Done", {}, "deny"),
    ("Authorize", {}, "deny"),
    ("Allow", {}, "deny"),
    ("Grant access", {}, "deny"),
    ("Continue with LinkedIn", {}, "deny"),
    (ZW, {}, "deny"),
    ("Ｓｕｂｍｉｔ", {}, "deny"),
    (CYR, {}, "deny"),
    ("Español", {}, "deny"),
    ("Next", {"type": "submit"}, "deny"),
    ("Next", {"type": "image"}, "deny"),
    ("Next", {"form_attrs": True}, "deny"),
    ("Next", {"disabled": True}, "deny"),
    ("", {"type": "submit"}, "deny"),
    ("Continue", {**LINK, "href": "https://jobs.example.com/apply/2"}, "allow"),
    ("Continue", {**LINK, "href": "https://other.example.com/apply/2"}, "deny"),
    ("Continue", {**LINK, "href": "https://jobs.example.com.evil.com/x"}, "deny"),
    ("Continue", {**LINK, "href": "https://evil.com/?h=jobs.example.com"}, "deny"),
    ("Continue", {**LINK, "href": "https://evil.com\\@jobs.example.com/"}, "deny"),
    ("Continue", {**LINK, "href": "https://user@jobs.example.com/"}, "deny"),
    ("Continue", {**LINK, "href": "javascript:void(0)"}, "deny"),
    ("Continue", {**LINK, "href": None}, "deny"),
    ("Continue", {**LINK, "href": "https://jobs.example.com/2", "target": "_blank"}, "deny"),
    ("Save & continue", {"tag": "a", "role": "button", "type": "", "href": "https://jobs.example.com/3"}, "allow"),
    ("Personal info", {"role": "tab", "tab_in_list": True}, "allow"),
    ("Review", {"role": "tab", "tab_in_list": True}, "deny"),
    ("Step 1", {"role": "tab", "tab_in_list": True, "selected": True}, None),
    ("Personal info", {"role": "tab", "tab_in_list": False}, None),
    ("About us", {}, None),
    ("Back", {}, None),
])
def test_decide(label, over, decision):
    got = clicks.decide(ctl(label, **over), HOST)
    assert (got[0] if got else None) == decision, got


@pytest.mark.parametrize("label,over,reason", [
    (ZW, {}, "label contains 'submit'"),
    ("Next", {"type": "submit"}, "type=submit"),
    (CYR, {}, "label has non-ASCII characters (possible look-alike)"),
    ("Continue", {**LINK, "href": "https://other.example.com/2"}, "link leaves the posting host"),
    ("Continue", {**LINK, "href": None}, "link without an href"),
    ("Personal info", {"role": "tab", "tab_in_list": True}, "step tab"),
    ("Next", {}, "navigation control"),
])
def test_decide_reasons(label, over, reason):
    assert clicks.decide(ctl(label, **over), HOST)[1] == reason


def test_deny_pattern_is_the_spec_list_plus_sso():
    for word in ("submit", "apply", "send", "finish", "review", "sign in", "signin", "log in", "login", "create",
                 "register", "accept", "agree", "upload", "attach", "pay", "confirm", "complete", "done",
                 "authorize", "authorise", "allow", "grant"):
        assert clicks.DENY_RE.search(word), word
    assert not clicks.DENY_RE.search("continue application")


def test_sso_pattern_contents():
    for word in ("linkedin", "google", "indeed", "microsoft", "apple", "facebook", "github", "sso", "oauth"):
        assert clicks.SSO_RE.search(word), word
    assert not clicks.SSO_RE.search("continue")


@pytest.mark.parametrize("label,over,decision,reason", [
    ("Next", {"type": "reset"}, "deny", "type=reset"),
    ("", {"role": "tab", "tab_in_list": True}, "deny", "no accessible label"),
    ("", {"role": "tab", "tab_in_list": True, "labels": ["Next"]}, "deny", "no accessible label"),
    ("Next", {"role": "menuitem"}, "deny", "role menuitem is not button/link/tab"),
    # a control may not say Next in one place and Submit in another
    ("Next", {"labels": ["Submit application"]}, "deny", "label contains 'submit'"),
    ("Next", {"labels": ["Next", "submit"]}, "deny", "label contains 'submit'"),
    ("Next", {"labels": ["Sign in with Google"]}, "deny", "label contains 'sign in'"),
    ("Next", {"labels": ["N\u0435xt"]}, "deny", "label has non-ASCII characters (possible look-alike)"),
    ("Next", {"bidi": True}, "deny", "label has bidirectional or format control characters"),
    ("timbus Next", {"bidi": True}, "deny", "label has bidirectional or format control characters"),
    ("Next", {"labels": ["Next", "Next"]}, "allow", "navigation control"),
])
def test_decide_extra(label, over, decision, reason):
    assert clicks.decide(ctl(label, **over), HOST) == (decision, reason)
