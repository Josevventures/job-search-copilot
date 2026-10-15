"""Regenerate the LinkedIn guest-page fixtures used by test_post_parser.py and test_intake.py.

The private version of these tests ran on saved copies of real public posts. These are invented posts with the
same structure: the og:title / og:description meta tags and the commentary <p> with LinkedIn-wrapped links
(redir/redirect?url=...) that lib/post_parser.py reads. Every name, company and link here is made up.

    python skills/job-apply-fleet/tests/fixtures/make_fixtures.py
"""
import html
from pathlib import Path
from urllib.parse import quote

OUT = Path(__file__).resolve().parent
P_OPEN = ('<p class="attributed-text-segment-list__content text-color-text !text-sm whitespace-pre-wrap break-words\n'
          '        " dir="ltr" data-test-id="main-feed-activity-card__commentary">')


def wrapped(url: str) -> str:
    """A link the way LinkedIn's guest page renders it: through the redir/redirect wrapper."""
    href = "https://www.linkedin.com/redir/redirect?url=" + quote(url, safe="") + "&amp;urlhash=AbCd&amp;trk=public_post-text"
    return (f'<a class="link" href="{href}" target="_self" rel="nofollow" '
            f'data-tracking-control-name="public_post-text" data-tracking-will-navigate>{html.escape(url)}</a>')


def mention(slug: str, name: str) -> str:
    """A company mention: a link to a LinkedIn company page, which is never a job link."""
    return (f'<a class="link" href="https://www.linkedin.com/company/{slug}?trk=public_post-text" target="_self" '
            f'data-tracking-control-name="public_post-text">{html.escape(name)}</a>')


def page(title: str, description: str, body: str) -> str:
    t, d = html.escape(title), html.escape(description)
    return ("<!DOCTYPE html>\n<html lang=\"en\">\n<head>\n<meta charset=\"utf-8\">\n"
            f"<meta property=\"og:title\" content=\"{t}\">\n<meta property=\"og:description\" content=\"{d}\">\n"
            "<title>LinkedIn</title>\n</head>\n<body>\n<main>\n<article>\n"
            f"{P_OPEN}{body}</p>\n</article>\n</main>\n</body>\n</html>\n")


def short(i: int, prefix: str) -> str:
    return f"https://lnkd.in/{prefix}{i:04d}xY"


def hiring_roundup() -> str:
    roles = ["Technical Program Manager", "Technical Program Manager", "Program Manager",
             "Infrastructure Program Manager", "Program Manager", "Technical Program Manager", "Program Manager"]
    lines = ["Brightfield Logistics is hiring for Program Management roles.", "",
             "If you're exploring your next step in program management, here are open roles worth a look:", ""]
    for i, r in enumerate(roles, 1):
        lines += [f"{i}. {r}", wrapped(short(i, "e")), ""]
    lines += ["Tailor your resume to each role, and reach out to people on the team.", "",
              "Note: I'm not the recruiter for these positions."]
    body = "\n".join(lines)
    return page("Brightfield Logistics is hiring for Program Management roles.… | Morgan Ellery | 23 comments",
                "Brightfield Logistics is hiring for Program Management roles.", body)


def vc_roundup() -> str:
    firms = [("harbor-ventures", "Harbor Ventures", "a Senior Investment Manager"),
             ("tidewater-capital", "Tidewater Capital", "2027 Analysts"),
             ("lanternworks", "Lanternworks", "a Researcher / Associate-in-Training"),
             ("ridgeback-holdings", "Ridgeback Holdings", "an Investment Intern"),
             ("ferncliff-fund", "Ferncliff Fund", "an Investor"),
             ("northgate-partners", "Northgate Partners", "an Associate"),
             ("stonefield-vc", "Stonefield VC", "Analysts"),
             ("quarry-collective", "Quarry Collective", "Fellows"),
             ("bluebell-group", "Bluebell Group", "a Senior Associate"),
             ("saltworks-ventures", "Saltworks Ventures", "an Associate"),
             ("meadowlark-capital", "Meadowlark Capital", "12 Analysts")]
    lines = ["11 venture firms hiring investors this week.", "",
             "Get these jobs direct to your inbox every week: " + wrapped(short(0, "g")), ""]
    for i, (slug, name, role) in enumerate(firms, 1):
        lines += [f"{mention(slug, name)} is hiring {role}. {wrapped(short(i, 'd'))}", ""]
    return page("11 venture firms hiring investors this week. | Avery Lindqvist",
                "11 venture firms hiring investors this week.", "\n".join(lines))


def pm_roundup() -> str:
    lines = ["25 early-career PM roles hiring right now (via " + wrapped("http://examplejobsboard.example") + "):", ""]
    for i in range(1, 26):
        lines += [f"{i}. {mention(f'company-{i:02d}', f'Company {i:02d}')} — Associate Product Manager "
                  f"({90 + i}K) {wrapped(short(i, 'p'))}", ""]
    lines += ["Browse all roles: " + wrapped("http://examplejobsboard.example")]
    return page("25 early-career PM roles hiring right now | Jordan Okonkwo | 41 comments",
                "25 early-career PM roles hiring right now", "\n".join(lines))


def single_post() -> str:
    body = ("I'm hiring on the Strategy &amp; Operations team at Brightfield Logistics! If you or someone you know "
            "wants to lead large-scale planning work, please apply here: " + wrapped(short(1, "s")))
    return page("Strategy & Operations Manager | Brightfield Logistics Careers | Avery Quinn",
                "I'm hiring on the Strategy & Operations team at Brightfield Logistics!", body)


def no_link_post() -> str:
    body = ("Harborview Partners is on a hiring push, including a new office with me in Boston. Roles below, "
            "please message me directly if interested.\n\n— Investments: Principal\n— Operations: BD; FinOps\n\n"
            "Entry to mid-level")
    return page("Harborview Partners is on a hiring push… | Casey Whitmore",
                "Harborview Partners is on a hiring push, including a new office with me in Boston.", body)


def main():
    files = {"hiring_roundup.html": hiring_roundup(), "vc_roundup.html": vc_roundup(), "pm_roundup.html": pm_roundup(),
             "single_post.html": single_post(), "no_link_post.html": no_link_post(),
             "login_wall.html": '<html><head><meta property="og:title" content="Sign Up | LinkedIn"></head><body></body></html>'}
    for name, text in files.items():
        (OUT / name).write_text(text, encoding="utf-8", newline="\n")
        print(name, len(text))


if __name__ == "__main__":
    main()
