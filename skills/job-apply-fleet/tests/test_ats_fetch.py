import io
import json

from lib import ats_fetch as af

URL = "https://acme.wd5.myworkdayjobs.com/en-US/External/job/New-York-NY/Head-of-Strategy_JR123?source=li"
API = "https://acme.wd5.myworkdayjobs.com/wday/cxs/acme/External/job/New-York-NY/Head-of-Strategy_JR123"
INFO = {"jobPostingInfo": {"title": "Head of Strategy", "location": "New York, NY",
                           "jobDescription": "<p>Own the <b>strategy</b> roadmap.</p><ul><li>Lead M&amp;A</li>"
                                             "<li>Partner with the CEO</li></ul>"}}


class Resp(io.BytesIO):
    def __enter__(self):
        return self

    def __exit__(self, *a):
        self.close()


class FakeOpener:
    def __init__(self, body=None, exc=None):
        self.body, self.exc, self.reqs = body, exc, []

    def open(self, req, timeout=None):
        self.reqs.append((req, timeout))
        if self.exc:
            raise self.exc
        return Resp(self.body if isinstance(self.body, bytes) else json.dumps(self.body).encode())


def test_cxs_url_maps_tenant_site_and_drops_locale_and_query():
    assert af.cxs_url(URL) == API
    assert af.cxs_url("https://acme.wd1.myworkdayjobs.com/Careers/job/Remote/Analyst_R9") == \
        "https://acme.wd1.myworkdayjobs.com/wday/cxs/acme/Careers/job/Remote/Analyst_R9"


def test_cxs_url_refuses_other_hosts_schemes_and_shapes():
    for bad in ("http://acme.wd5.myworkdayjobs.com/en-US/External/job/x",       # not https
                "https://acme.wd5.myworkdayjobs.com.evil.com/External/job/x",    # suffix spoof
                "https://evilmyworkdayjobs.com/External/job/x",                  # no dot before the domain
                "https://evil.com\.myworkdayjobs.com/External/job/x",           # backslash in the host
                "https://acme_x.wd5.myworkdayjobs.com/External/job/x",           # outside [a-z0-9-]
                "https://boards.greenhouse.io/acme/jobs/1",
                "https://acme.wd5.myworkdayjobs.com/en-US/External",             # no /job/
                "https://acme.wd5.myworkdayjobs.com/en-US/job/x",                # no site
                "not a url", None):
        assert af.cxs_url(bad) is None, bad


def test_is_workday():
    assert af.is_workday(URL) and not af.is_workday("https://a.com/job/1") and not af.is_workday(None)


def test_fetch_returns_plain_text_with_get_and_timeout():
    op = FakeOpener(INFO)
    text = af.fetch_workday_jd(URL, opener=op)
    assert text.splitlines()[0] == "Head of Strategy" and "New York, NY" in text
    assert "Own the strategy roadmap." in text and "Lead M&A" in text and "<" not in text
    req, timeout = op.reqs[0]
    assert req.full_url == API and req.get_method() == "GET" and timeout == af.TIMEOUT_S
    assert not req.has_header("Cookie") and not req.has_header("Authorization")


def test_fetch_never_raises_and_returns_none_on_any_failure():
    logs = []
    assert af.fetch_workday_jd(URL, opener=FakeOpener(exc=OSError("down")), log=logs.append) is None
    assert af.fetch_workday_jd(URL, opener=FakeOpener(b"not json")) is None
    assert af.fetch_workday_jd(URL, opener=FakeOpener({"jobPostingInfo": {"title": "x"}})) is None   # no description
    assert af.fetch_workday_jd(URL, opener=FakeOpener([1, 2])) is None
    assert af.fetch_workday_jd("https://a.com/job/1", opener=FakeOpener(INFO)) is None              # never called
    assert logs and "OSError" in logs[0] and len(logs[0]) <= 200


def test_fetch_caps_the_response_at_2_mb():
    big = json.dumps({"jobPostingInfo": {"title": "t", "jobDescription": "x" * (af.MAX_BYTES + 10)}}).encode()
    assert af.fetch_workday_jd(URL, opener=FakeOpener(big)) is None


def test_default_opener_refuses_redirects_off_workday():
    h = af._SameHostRedirect()
    import urllib.error
    import urllib.request
    req = urllib.request.Request(API)
    try:
        h.redirect_request(req, None, 302, "Found", {}, "https://evil.com/x")
        raise AssertionError("redirect off Workday was followed")
    except urllib.error.HTTPError:
        pass
    assert h.redirect_request(req, None, 302, "Found", {}, API + "?x=1") is not None


def test_is_workday_needs_a_clean_hostname():
    assert not af.is_workday("https://evil.com\.myworkdayjobs.com/x")
    assert not af.is_workday("https://.myworkdayjobs.com/x") and not af.is_workday("https://a..myworkdayjobs.com/x")
    assert af.is_workday("https://ACME.wd5.myworkdayjobs.com/x")


def test_fetch_gives_up_at_a_total_15_s_deadline(monkeypatch):
    """Review 4: a server that trickles bytes cannot hold the fetch past 15 s in total."""
    ticks = iter(range(0, 1000, 10))                                        # each read costs 10 s
    monkeypatch.setattr(af.time, "monotonic", lambda: next(ticks))
    body = json.dumps(INFO).encode() + b" " * (3 * af.CHUNK)
    assert af.fetch_workday_jd(URL, opener=FakeOpener(body)) is None


def test_fetch_reads_in_chunks_and_keeps_the_2_mb_cap():
    big = json.dumps({"jobPostingInfo": {"title": "t", "jobDescription": "x" * (af.MAX_BYTES + 10)}}).encode()
    assert af.fetch_workday_jd(URL, opener=FakeOpener(big)) is None
    assert af.fetch_workday_jd(URL, opener=FakeOpener(json.dumps(INFO).encode() + b" " * (3 * af.CHUNK)))


class NoBlockingRead(Resp):
    def read(self, *a):
        raise AssertionError("read() can block for many socket reads; use read1()")


def test_fetch_uses_read1_so_one_call_is_one_socket_read():
    """Re-review 4: BufferedReader.read(n) loops raw reads until n bytes, defeating the deadline; read1 does one."""
    class Opener(FakeOpener):
        def open(self, req, timeout=None):
            return NoBlockingRead(json.dumps(INFO).encode())
    assert "Head of Strategy" in af.fetch_workday_jd(URL, opener=Opener())
