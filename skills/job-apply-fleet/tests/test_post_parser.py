from conftest import FIXTURES
from lib.post_parser import parse_post, is_job_link, candidate_links, is_roundup

# Fixtures are invented posts with the structure of LinkedIn's guest page (fixtures/make_fixtures.py).


def load(name):
    return parse_post((FIXTURES / f"{name}.html").read_text(encoding="utf-8"))


def test_hiring_roundup_has_7_links_and_hints():
    p = load("hiring_roundup")
    c = candidate_links(p)
    assert is_roundup(p) and len(c) == 7
    assert "Program Manager" in c[0][1]


def test_vc_roundup_includes_newsletter_link_plus_firms_and_skips_company_mentions():
    c = candidate_links(load("vc_roundup"))
    assert len(c) >= 10
    assert not [u for u, _ in c if "/company/" in u]


def test_pm_roundup_has_25():
    assert len(candidate_links(load("pm_roundup"))) == 25


def test_single_post_not_roundup_and_has_its_one_link():
    p = load("single_post")
    assert not is_roundup(p)
    assert any("lnkd.in" in u for u, _ in candidate_links(p))
    assert p.poster_name and "Quinn" in p.poster_name


def test_no_link_post_has_body_and_no_candidates():
    p = load("no_link_post")
    assert "Harborview" in p.body and candidate_links(p) == []


def test_login_wall_detected():
    p = load("login_wall")
    assert p.login_walled and p.body == ""


def test_is_job_link():
    yes = ["https://jobs.ashbyhq.com/exampleco/1ef5", "https://job-boards.greenhouse.io/examplecorp/jobs/4000000001",
           "https://exampleco.wd1.myworkdayjobs.com/x/job/y", "https://www.linkedin.com/jobs/view/4000000000",
           "https://careers.example.org/jobs/researcher", "https://jobright.ai/jobs/info/6a8e",
           "https://apply.workable.com/example-fund/", "https://www.example.org/job/senior-investment-analyst/"]
    no = ["https://www.linkedin.com/company/example-ventures", "https://www.linkedin.com/feed/hashtag/pmjobs",
          "https://www.linkedin.com/signup/cold-join?x=1", "https://youtu.be/aaaaaaaaaaa", "https://examplejobsboard.com"]
    assert all(is_job_link(u) for u in yes), [u for u in yes if not is_job_link(u)]
    assert not any(is_job_link(u) for u in no), [u for u in no if is_job_link(u)]
