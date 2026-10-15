from lib.canonical import canonicalize, lead_id, unwrap_static, is_shortlink, is_linkedin_post, is_x_post

SAFETY_WRAPPED = ("https://www.linkedin.com/safety/go/?url=https%3A%2F%2Fcareers%2Eexamplefirm%2Ecom%2Fcareers%2F"
        "JobDetail%3FjobId%3D400123%26source%3DJB-90000&urlhash=AbCd&mt=12-a3B&isSdui=true")


def test_safety_go_unwraps_and_keeps_functional_params():
    assert unwrap_static(SAFETY_WRAPPED) == "https://careers.examplefirm.com/careers/JobDetail?jobId=400123&source=JB-90000"
    assert canonicalize(SAFETY_WRAPPED) == "https://careers.examplefirm.com/careers/JobDetail?jobId=400123&source=JB-90000"


def test_redir_redirect_unwraps():
    u = "https://www.linkedin.com/redir/redirect?url=https%3A%2F%2Flnkd%2Ein%2Fed6Ci7Xj&urlhash=VudB&trk=public_post-text"
    assert unwrap_static(u) == "https://lnkd.in/ed6Ci7Xj"


def test_cold_join_unwraps_to_inner_post():
    u = ("https://www.linkedin.com/signup/cold-join?session_redirect=https%3A%2F%2Fwww.linkedin.com%2Ffeed%2F"
         "update%2Furn%3Ali%3Ashare%3A7000000000000000004%3Futm_source%3Dshare")
    assert canonicalize(u) == "https://www.linkedin.com/feed/update/urn:li:share:7000000000000000004"


def test_tracking_params_stripped_and_trailing_slash():
    u = ("https://www.linkedin.com/posts/example-poster_manager-product-share-7000000000000000003-0dYW/"
         "?utm_source=share&utm_medium=member_android&rcm=ACoAAAt")
    assert canonicalize(u) == "https://www.linkedin.com/posts/example-poster_manager-product-share-7000000000000000003-0dYW"


def test_jobright_utm_campaign_stripped():
    assert canonicalize("https://jobright.ai/jobs/info/6a8ed5e?utm_source=1115&utm_campaign=roundup") == \
        "https://jobright.ai/jobs/info/6a8ed5e"


def test_host_lowercased_path_case_kept_and_x_s_param_stripped():
    assert canonicalize("HTTPS://X.com/example_user/status/2100000000000000001?s=20") == \
        "https://x.com/example_user/status/2100000000000000001"
    assert canonicalize("https://jobs.ashbyhq.com/ExampleAI/f0c8") == "https://jobs.ashbyhq.com/ExampleAI/f0c8"


def test_source_param_dropped_only_when_tracking_value():
    assert canonicalize("https://a.com/job?id=1&source=share") == "https://a.com/job?id=1"
    assert canonicalize("https://a.com/job?id=1&source=JB-90000") == "https://a.com/job?id=1&source=JB-90000"


def test_whitespace_and_fragment():
    assert canonicalize("  https://a.com/job/1#apply ") == "https://a.com/job/1"


def test_lead_id_is_stable_12_hex():
    a = lead_id("https://a.com/job/1")
    assert a == lead_id("https://a.com/job/1") and len(a) == 12
    assert all(c in "0123456789abcdef" for c in a)


def test_predicates():
    assert is_shortlink("https://lnkd.in/p/gG7m_Tgy")
    assert is_linkedin_post("https://www.linkedin.com/posts/x_y-share-1-abc")
    assert is_linkedin_post("https://www.linkedin.com/feed/update/urn:li:share:1")
    assert not is_linkedin_post("https://www.linkedin.com/jobs/view/4000000001")
    assert is_x_post("https://x.com/a/status/1") and is_x_post("https://twitter.com/a/status/1")
