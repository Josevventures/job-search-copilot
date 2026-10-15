"""Site adapters for the DOM path (addendum §3.2): Lever and Ashby rewrite a posting URL to its application page
and give a label hint for their question cards; GENERIC reads any other page as it is."""
import re
from dataclasses import dataclass

_UUID = r"[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}"


@dataclass(frozen=True)
class Adapter:
    name: str
    ats: str | None = None
    pattern: re.Pattern | None = None
    suffix: str = ""
    hint: dict | None = None

    def matches(self, url) -> bool:
        return bool(self.pattern and self.pattern.match(str(url or "")))

    def apply_url(self, url) -> str:
        m = self.pattern.match(str(url or "")) if self.pattern else None
        if not m:
            return str(url)
        return f"https://{m.group('host').lower()}/{m.group('org')}/{m.group('job')}{self.suffix}"


LEVER = Adapter("lever_dom", "lever",
                re.compile(rf"^https?://(?P<host>jobs(?:\.eu)?\.lever\.co)/(?P<org>[^/?#]+)/(?P<job>{_UUID})"
                           r"(?:/apply)?/?(?:[?#].*)?$", re.I),
                "/apply", {"container": ".application-question", "title": ".application-label", "single_page": True})
ASHBY = Adapter("ashby_dom", "ashby",
                re.compile(rf"^https?://(?P<host>jobs\.ashbyhq\.com)/(?P<org>[^/?#]+)/(?P<job>{_UUID})"
                           r"(?:/application)?/?(?:[?#].*)?$", re.I),
                "/application", {"container": '[class*="ashby-application-form-field-entry"], [class*="ashby-application-form-autofill-pane"], '
                                              # a radio group's entry is a bare div[data-field-path] around a
                                              # legend-less <fieldset> whose title is a sibling <label>
                                              '[data-field-path]',
                                 "title": '[class*="ashby-application-form-question-title"], h3',
                                 "single_page": True})
GENERIC = Adapter("generic_dom")
SITE_ADAPTERS = (LEVER, ASHBY)
_ATS_HOSTS = (("greenhouse.io", "greenhouse"), ("lever.co", "lever"), ("ashbyhq.com", "ashby"),
              ("myworkdayjobs.com", "workday"), ("myworkdaysite.com", "workday"), ("icims.com", "icims"),
              ("linkedin.com", "linkedin"), ("smartrecruiters.com", "smartrecruiters"), ("workable.com", "workable"))


def match(url) -> Adapter | None:
    return next((a for a in SITE_ADAPTERS if a.matches(url)), None)


def ats_for(host) -> str | None:
    h = str(host or "").lower()
    return next((name for suffix, name in _ATS_HOSTS if h == suffix or h.endswith("." + suffix)), None)
