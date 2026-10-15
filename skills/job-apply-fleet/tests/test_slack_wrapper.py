import json
from datetime import datetime, timedelta, timezone

import pytest

import jobs_slack_intake as w
from lib import slack_intake as si
from lib.lead_store import LeadStore

U = "U0TESTUSER1"
NOW = datetime(2026, 10, 15, 11, 30, tzinfo=timezone.utc)   # 07:30 ET: outside quiet hours
NIGHT = "2026-10-15"
_HOLDER = {}
REAL_RUN = w.run


class FakeSlack:
    def __init__(self, messages):
        self.messages, self.posts, self.threads = messages, [], {}

    def history(self, oldest):
        return [m for m in self.messages if float(m["ts"]) >= float(oldest)]

    def replies(self, ts):
        return self.threads.get(ts, [])

    def post(self, text, thread_ts=None):
        self.posts.append((thread_ts, text))
        return f"9{len(self.posts)}.0"


def fake_ingest(store):
    def ingest(urls, via, st, note=None, slack=None):
        by_url = {}
        for u in urls:
            src = {"url": u, "kind": "posting", "poster_name": None, "poster_url": None, "added_via": via,
                   "roundup_of": None, "hint": None, "note": note, "slack": slack}
            lid, _ = st.create_or_merge(u, src, jd_links=[u])
            by_url[u] = {"status": "ok", "lead_ids": [lid], "roundup": False}
        return {"by_url": by_url, "failed": []}
    return ingest


def triaging_runner(store, take):
    """Marks the first `take` queued leads complete on the board, writes result.json like fleet_wave result."""
    calls = []

    def runner(ids_path, result_path, limit):
        ids = ids_path.read_text(encoding="utf-8").split()
        calls.append(ids)
        done, carried = ids[:take], ids[take:]
        for lid in done:
            store.update(lid, "triage", {"triage_status": "complete", "route": "board", "company": "Acme",
                                         "role_title": "Lead", "jd_read": True})
        res = {"batch_id": "b", "triaged": done, "failed": [], "carried": carried, "airtable_dedupe": "ok",
               "stopped": False}
        result_path.write_text(json.dumps(res), encoding="utf-8")
        return {"ok": True, **res}
    runner.calls = calls
    return runner


@pytest.fixture(autouse=True)
def _ingest(monkeypatch):
    holder = _HOLDER
    monkeypatch.setattr(w.leads_intake, "ingest", lambda *a, **k: holder["fn"](*a, **k))
    return holder


@pytest.fixture(autouse=True)
def _no_network(monkeypatch):
    """No test reaches Workday: the default prefetcher is replaced by one that fails loudly."""
    def refuse(url, log=None):
        raise AssertionError(f"real Workday fetch attempted: {url}")
    monkeypatch.setattr(w.ats_fetch, "fetch_workday_jd", refuse)


@pytest.fixture(autouse=True)
def _intake_dir(tmp_path, monkeypatch):
    """Controller ruling C5: no test writes the real slack-intake runs.log / state.json."""
    monkeypatch.setenv("JOB_SLACK_INTAKE_DIR", str(tmp_path / "si"))


def go(tmp_path, slack, store, runner, scorer=lambda leads, now: {}, now=NOW, post=True, sleeps=None,
       triage_minutes=0):
    led = si.Ledger(tmp_path / si.LEDGER_NAME)
    _HOLDER["fn"] = fake_ingest(store)
    out = w.run(slack=slack, store=store, ledger=led, user_id=U, channel="CJ", now=now, post=post, runner=runner,
                scorer=scorer, sleep=(sleeps.append if sleeps is not None else lambda s: None),
                run_dir=tmp_path / "run", clock=lambda: now + timedelta(minutes=triage_minutes))
    return out, led


def m(ts, text):
    return {"ts": ts, "user": U, "text": text}


def test_end_to_end_final_reply_and_ledger(tmp_dirs, tmp_path):
    s = LeadStore()
    slack = FakeSlack([m("1792000000.000100", "Dana's team <https://a.com/job/1|Lead>")])
    runner = triaging_runner(s, take=20)
    out, led = go(tmp_path, slack, s, runner, scorer=lambda leads, now: {l["lead_id"]: 82 for l in leads})
    assert len(slack.posts) == 1
    ts, text = slack.posts[0]
    assert ts == "1792000000.000100" and text.splitlines()[0].endswith(si.FINAL_TOKEN) and "score 82" in text
    assert led.state()["1792000000.000100"]["final"]
    lead = s.read(runner.calls[0][0])
    assert lead["intake"]["sources"][0]["note"] == "Dana's team"
    # second run the same night: nothing to do, no Claude run, no post
    out2, _ = go(tmp_path, slack, s, runner)
    assert len(slack.posts) == 1 and len(runner.calls) == 1


def test_over_cap_partial_then_followup_final_next_night(tmp_dirs, tmp_path):
    s = LeadStore()
    slack = FakeSlack([m("1792000000.000100", "https://a.com/job/1 https://a.com/job/2 https://a.com/job/3")])
    go(tmp_path, slack, s, triaging_runner(s, take=2))
    assert "queued (over tonight's cap)" in slack.posts[0][1] and si.FINAL_TOKEN not in slack.posts[0][1]
    nxt = datetime(2026, 10, 16, 11, 30, tzinfo=timezone.utc)
    runner2 = triaging_runner(s, take=20)
    go(tmp_path, slack, s, runner2, now=nxt)
    assert len(runner2.calls[0]) == 1                           # only the carried lead is re-queued
    follow = slack.posts[1][1]
    assert si.FINAL_TOKEN in follow and follow.count("✅") == 1   # covers only the carried lead


def test_failed_headless_run_posts_unfinished_and_requeues(tmp_dirs, tmp_path):
    s = LeadStore()
    slack = FakeSlack([m("1792000000.000100", "https://a.com/job/1")])
    go(tmp_path, slack, s, lambda ids_path, result_path, limit: {"ok": False, "error": "timed out"})
    assert "retrying next run" in slack.posts[0][1]
    # unchanged pending set on the next failing night -> no second partial reply
    nxt = datetime(2026, 10, 16, 11, 30, tzinfo=timezone.utc)
    go(tmp_path, slack, s, lambda ids_path, result_path, limit: {"ok": False, "error": "timed out"}, now=nxt)
    assert len(slack.posts) == 1


def test_duplicate_reply_guard(tmp_dirs, tmp_path):
    s = LeadStore()
    ts = "1792000000.000100"
    slack = FakeSlack([m(ts, "https://a.com/job/1")])
    slack.threads[ts] = [{"ts": ts}, {"ts": "2.0", "bot_id": "B1", "text": f"Intake done {si.FINAL_TOKEN}"}]
    out, led = go(tmp_path, slack, s, triaging_runner(s, take=20))
    assert slack.posts == [] and led.state()[ts]["final"]


def test_same_link_in_two_messages_is_queued_once(tmp_dirs, tmp_path):
    s = LeadStore()
    slack = FakeSlack([m("1792000000.000100", "https://a.com/job/1"),
                       m("1792000000.000200", "again https://a.com/job/1")])
    runner = triaging_runner(s, take=20)
    go(tmp_path, slack, s, runner)
    assert len(runner.calls[0]) == 1 and len(slack.posts) == 2
    (ts1, t1), (ts2, t2) = slack.posts
    assert ts1 == "1792000000.000100" and ts2 == "1792000000.000200"
    assert "✅ Acme · Lead" in t1 and "✅ Acme · Lead" in t2 and si.FINAL_TOKEN in t2   # both threads show the lead


def test_edited_message_with_a_new_link_ingests_only_the_new_one(tmp_dirs, tmp_path):
    s = LeadStore()
    ts = "1792000000.000100"
    slack = FakeSlack([m(ts, "https://a.com/job/1")])
    go(tmp_path, slack, s, triaging_runner(s, take=0))            # night 1: pending, partial reply
    slack.messages = [m(ts, "https://a.com/job/1 https://a.com/job/2")]
    runner2 = triaging_runner(s, take=20)
    go(tmp_path, slack, s, runner2, now=datetime(2026, 10, 16, 11, 30, tzinfo=timezone.utc))
    assert len(runner2.calls[0]) == 2 and si.FINAL_TOKEN in slack.posts[-1][1]
    assert len(slack.posts) == 2 and slack.posts[-1][1].count("✅") == 2   # the new link is reported too


def _down(text, thread_ts=None):
    raise ConnectionError("slack down")


def test_same_night_rerun_after_a_crash_picks_up_a_link_added_by_an_edit(tmp_dirs, tmp_path):
    """Regression (task 7 concern 1): the first run crashes before posting, the user edits the message to add a link,
    and the same night re-runs. The new link must be recorded, triaged and reported - never orphaned."""
    s = LeadStore()
    ts = "1792000000.000100"
    slack = FakeSlack([m(ts, "https://a.com/job/1")])

    slack.post = _down                                              # run 1 crashes before any reply row
    with pytest.raises(ConnectionError):
        go(tmp_path, slack, s, triaging_runner(s, take=0))
    del slack.post
    assert slack.posts == []
    slack.messages = [m(ts, "https://a.com/job/1 https://a.com/job/2")]
    runner = triaging_runner(s, take=20)
    out, led = go(tmp_path, slack, s, runner)                       # same night (NOW)
    st = led.state()[ts]
    assert set(st["by_url"]) == {"https://a.com/job/1", "https://a.com/job/2"}
    new_lid = st["by_url"]["https://a.com/job/2"]["lead_ids"][0]
    assert new_lid in runner.calls[0] and len(runner.calls[0]) == 2
    assert s.read(new_lid)["triage"]["triage_status"] == "complete"
    assert len(slack.posts) == 1 and si.FINAL_TOKEN in slack.posts[0][1] and slack.posts[0][1].count("✅") == 2


def test_same_night_rerun_with_an_over_cap_new_link_is_never_final(tmp_dirs, tmp_path):
    """The same-night re-run reports the added link as queued, so the reply is not final while it is pending."""
    s = LeadStore()
    ts = "1792000000.000100"
    slack = FakeSlack([m(ts, "https://a.com/job/1")])

    slack.post = _down                                              # run 1 crashes before any reply row
    with pytest.raises(ConnectionError):
        go(tmp_path, slack, s, triaging_runner(s, take=0))
    del slack.post
    slack.messages = [m(ts, "https://a.com/job/1 https://a.com/job/2")]
    out, led = go(tmp_path, slack, s, triaging_runner(s, take=1))
    text = slack.posts[0][1]
    assert si.FINAL_TOKEN not in text and "queued (over tonight's cap)" in text
    assert not led.state()[ts]["final"]


def test_no_link_message_gets_one_final_reply_and_no_claude_run(tmp_dirs, tmp_path):
    s = LeadStore()
    slack = FakeSlack([m("1792000000.000100", "remind me to look at Brightfield")])
    runner = triaging_runner(s, take=20)
    go(tmp_path, slack, s, runner)
    assert runner.calls == [] and slack.posts[0][1] == f"No link found in this message. {si.FINAL_TOKEN}"


def test_quiet_hours_wait_and_dry_run(tmp_dirs, tmp_path, capsys):
    s = LeadStore()
    early = datetime(2026, 10, 15, 10, 10, tzinfo=timezone.utc)   # 06:10 ET
    sleeps = []
    slack = FakeSlack([m("1792000000.000100", "https://a.com/job/1")])
    go(tmp_path, slack, s, triaging_runner(s, take=20), now=early, sleeps=sleeps)
    assert sleeps == [50 * 60]
    s2 = LeadStore()
    slack2, sleeps2 = FakeSlack([m("1792000000.000300", "https://a.com/job/9")]), []
    out, led = go(tmp_path / "dry", slack2, s2, triaging_runner(s2, take=20), now=early, post=False, sleeps=sleeps2)
    assert sleeps2 == [] and slack2.posts == [] and "WOULD REPLY" in capsys.readouterr().out
    assert not led.state()["1792000000.000300"]["final"]


def test_long_quiet_wait_defers_replies_to_the_next_run(tmp_dirs, tmp_path, capsys):
    """Ruling C9: an evening catch-up run (22:30 ET) triages but posts nothing and writes no replied rows; the next
    05:30 run posts after its own 07:00 hold. A dry run never waits."""
    s = LeadStore()
    ts = "1792000000.000100"
    evening = datetime(2026, 10, 16, 2, 30, tzinfo=timezone.utc)   # 2026-10-15 22:30 ET -> 8.5 h to 07:00
    sleeps = []
    slack = FakeSlack([m(ts, "https://a.com/job/1")])
    runner = triaging_runner(s, take=20)
    out, led = go(tmp_path, slack, s, runner, now=evening, sleeps=sleeps)
    assert sleeps == [] and slack.posts == [] and out["replies"] == 0 and out["deferred"]
    assert len(runner.calls) == 1                                   # triage still ran
    assert [r["state"] for r in led.rows()] == ["ingested"]         # no replied row
    morning = datetime(2026, 10, 16, 9, 30, tzinfo=timezone.utc)    # 05:30 ET -> 90 min hold, then post
    out2, led = go(tmp_path, slack, s, runner, now=morning, sleeps=sleeps)
    assert sleeps == [90 * 60] and len(slack.posts) == 1 and si.FINAL_TOKEN in slack.posts[0][1]
    assert len(runner.calls) == 1 and led.state()[ts]["final"] and not out2["deferred"]
    # dry run in the evening: no wait, no deferral, prints the would-be reply
    s3 = LeadStore()
    slack3, sleeps3 = FakeSlack([m("1792000000.000300", "https://a.com/job/9")]), []
    go(tmp_path / "dry", slack3, s3, triaging_runner(s3, take=20), now=evening, post=False, sleeps=sleeps3)
    assert sleeps3 == [] and "WOULD REPLY" in capsys.readouterr().out


def test_catch_up_run_at_0330_with_90_min_of_triage_defers(tmp_dirs, tmp_path):
    """Ruling C9b: elapsed (90 min) + quiet wait (2 h, 05:00 -> 07:00) = 3.5 h > 2.5 h: post nothing this run."""
    s = LeadStore()
    sleeps = []
    slack = FakeSlack([m("1792000000.000100", "https://a.com/job/1")])
    start = datetime(2026, 10, 16, 7, 30, tzinfo=timezone.utc)      # 03:30 ET
    out, led = go(tmp_path, slack, s, triaging_runner(s, take=20), now=start, sleeps=sleeps, triage_minutes=90)
    assert out["deferred"] and sleeps == [] and slack.posts == []
    assert [r["state"] for r in led.rows()] == ["ingested"]


def test_normal_0530_run_with_60_min_of_triage_holds_then_posts(tmp_dirs, tmp_path):
    """Ruling C9b: elapsed 60 min + wait 30 min (06:30 -> 07:00) = 1.5 h: hold until 07:00, then post."""
    s = LeadStore()
    sleeps = []
    slack = FakeSlack([m("1792000000.000100", "https://a.com/job/1")])
    start = datetime(2026, 10, 16, 9, 30, tzinfo=timezone.utc)      # 05:30 ET
    out, led = go(tmp_path, slack, s, triaging_runner(s, take=20), now=start, sleeps=sleeps, triage_minutes=60)
    assert not out["deferred"] and sleeps == [30 * 60] and len(slack.posts) == 1
    assert si.FINAL_TOKEN in slack.posts[0][1]


def test_daytime_catch_up_run_posts_without_waiting(tmp_dirs, tmp_path):
    s = LeadStore()
    sleeps = []
    slack = FakeSlack([m("1792000000.000100", "https://a.com/job/1")])
    start = datetime(2026, 10, 15, 14, 0, tzinfo=timezone.utc)      # 10:00 ET
    out, _ = go(tmp_path, slack, s, triaging_runner(s, take=20), now=start, sleeps=sleeps, triage_minutes=45)
    assert sleeps == [] and not out["deferred"] and len(slack.posts) == 1


def test_a_raising_runner_becomes_a_failed_run_with_retry_replies(tmp_dirs, tmp_path):
    """Review fix 1: e.g. `claude` missing from PATH under Task Scheduler. The run reports ok=False, keeps the batch,
    and the reply says the lead is retried next run."""
    s = LeadStore()
    slack = FakeSlack([m("1792000000.000100", "https://a.com/job/1")])

    def missing(ids_path, result_path, limit):
        raise FileNotFoundError("claude")
    out, _ = go(tmp_path, slack, s, missing)
    assert out["run"]["ok"] is False and "FileNotFoundError" in out["run"]["error"]
    assert out["batch"] == "slack-run" and "retrying next run" in slack.posts[0][1]


def test_scores_for_returns_none_when_bridge_fails(tmp_path):
    class Bad:
        @staticmethod
        def lead_to_view(l):
            return l

        @staticmethod
        def rank(*a, **k):
            raise RuntimeError("node missing")
    cfg = tmp_path / "cfg.json"
    cfg.write_text("{}", encoding="utf-8")
    assert w.scores_for([{"lead_id": "a"}], NOW, bridge=Bad, config_path=cfg) is None
    assert "scorer unavailable" in (tmp_path / "si" / "runs.log").read_text(encoding="utf-8")


def test_run_headless_strips_api_key_and_reads_result(tmp_path, monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-test")
    monkeypatch.delenv("JSC_KEEP_API_KEY", raising=False)
    seen = {}

    def fake_run(cmd, **kw):
        seen["cmd"], seen["env"] = cmd, kw["env"]
        (tmp_path / "result.json").write_text(json.dumps({"carried": [], "stopped": False}), encoding="utf-8")

        class P:
            returncode, stdout = 0, b"{}"
        return P()
    ids = tmp_path / "ids.txt"
    ids.write_text("a\n", encoding="utf-8")
    res = w.run_headless(ids, tmp_path / "result.json", 20, run=fake_run)
    assert res["ok"] is True and "ANTHROPIC_API_KEY" not in seen["env"]
    assert seen["cmd"][:5] == ["claude", "-p", "--model", "sonnet", "--output-format"]
    assert "--limit 20" in seen["cmd"][6]


def test_run_headless_prompt_names_the_batch_and_merges_previous_first(tmp_path):
    """Ruling C2: literal batch id (from the run dir) and literal venv path; a previous failed batch is merged first."""
    seen = {}

    def fake_run(cmd, **kw):
        seen["prompt"] = cmd[6]
        raise w.subprocess.TimeoutExpired(cmd, 1)
    rd = tmp_path / "20261015-093000"
    rd.mkdir()
    res = w.run_headless(rd / "ids.txt", rd / "result.json", 20, run=fake_run,
                         merge_first=("slack-20261014-093000",))
    assert res["ok"] is False
    p = seen["prompt"]
    assert w.batch_id_for(rd) == "slack-20261015-093000"
    assert "--batch slack-20261015-093000" in p and "$" not in p
    merge = f"{w.VENV_PY} skills/job-apply-fleet/fleet_merge.py --batch slack-20261014-093000"
    assert merge in p and p.index(merge) < p.index("fleet_wave.py init")
    w.run_headless(rd / "ids.txt", rd / "result.json", 20, run=fake_run)
    assert "fleet_merge.py --batch" not in seen["prompt"].split("fleet_wave.py init")[0]


def test_main_without_channel_id_exits_2(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(w, "_load_slack_config", lambda: {"enabled": True, "user_id": U})
    monkeypatch.setattr(w.config, "enabled", lambda name, cfg=None: True)
    assert w.main([]) == 2 and "channel_id / user_id not set" in capsys.readouterr().out


def test_main_with_the_modules_off_exits_2(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(w, "_load_slack_config", lambda: {"enabled": False, "user_id": U, "channel_id": "CJ"})
    assert w.main([]) == 2 and "must both be enabled" in capsys.readouterr().out


# ---- main(): health, failure alert, unmerged batches ----------------------------------------------

@pytest.fixture
def main_env(tmp_dirs, tmp_path, monkeypatch):
    monkeypatch.setattr(w, "_load_slack_config",
                        lambda: {"enabled": True, "user_id": U, "channel_id": "CJ", "timezone": "America/New_York"})
    monkeypatch.setattr(w.config, "enabled", lambda name, cfg=None: True)
    monkeypatch.setattr(w, "_token", lambda: None)
    slack = FakeSlack([])
    monkeypatch.setattr(w, "Slack", lambda channel, token=None: slack)
    env = {"slack": slack, "calls": [], "results": [], "now": None}
    monkeypatch.setattr(w, "_utcnow", lambda: env["now"])

    def fake_run(**kw):
        env["calls"].append(kw)
        return env["results"].pop(0)
    monkeypatch.setattr(w, "run", fake_run)
    env["health"] = si.Health(tmp_path / "si" / "state.json")
    return env


def _summary(ok, batch="slack-x", skipped=False):
    run = {"ok": ok, "error": None if ok else "timed out", "skipped": skipped}
    return {"messages": 1, "ingested": 0, "queued": 0 if skipped else 1, "replies": 0, "stale": [],
            "batch": None if skipped else batch, "deferred": False, "run": run}


def test_two_failed_headless_nights_trigger_the_failure_alert(main_env):
    """Ruling C6: a failed headless run is not a successful night, so two in a row alert the user."""
    env = main_env
    env["health"].success("2026-10-13")
    env["now"] = datetime(2026, 10, 14, 9, 30, tzinfo=timezone.utc)
    env["results"].append(_summary(False))
    assert w.main([]) == 0 and env["slack"].posts == []
    assert env["health"].gap_nights("2026-10-14") == 1           # night 1's failure did not count as success
    env["now"] = datetime(2026, 10, 15, 9, 30, tzinfo=timezone.utc)
    env["results"].append(_summary(False, batch="slack-y"))
    assert w.main([]) == 0
    assert len(env["slack"].posts) == 1
    thread, text = env["slack"].posts[0]
    assert thread is None and "had not run successfully since 2026-10-13" in text
    env["now"] = datetime(2026, 10, 16, 9, 30, tzinfo=timezone.utc)
    env["results"].append(_summary(True))
    w.main([])
    assert env["health"].gap_nights("2026-10-16") == 0


def test_failed_batch_is_merged_by_the_next_run_then_cleared(main_env):
    """Ruling C2: the failed run's batch is recorded and handed to the next run's prompt, then cleared on success."""
    env = main_env
    env["now"] = datetime(2026, 10, 14, 9, 30, tzinfo=timezone.utc)
    env["results"].append(_summary(False, batch="slack-20261014-093000"))
    w.main([])
    assert env["calls"][0]["runner"].keywords["merge_first"] == ()
    assert env["health"].unmerged() == ["slack-20261014-093000"]
    env["now"] = datetime(2026, 10, 15, 9, 30, tzinfo=timezone.utc)
    env["results"].append(_summary(True, skipped=True))           # nothing queued: keep it for a real run
    w.main([])
    assert env["health"].unmerged() == ["slack-20261014-093000"]
    env["now"] = datetime(2026, 10, 16, 9, 30, tzinfo=timezone.utc)
    env["results"].append(_summary(True, batch="slack-20261016-093000"))
    w.main([])
    assert env["calls"][2]["runner"].keywords["merge_first"] == ("slack-20261014-093000",)
    assert env["health"].unmerged() == []


def test_batch_is_recorded_when_run_raises_after_starting_it(main_env):
    """Review fix 1 (ruling C2, crash path): a batch started before an exception (e.g. a Slack post error after a
    failed headless run) is still recorded for the next run to merge, and the night is not a success."""
    env = main_env
    env["now"] = datetime(2026, 10, 14, 9, 30, tzinfo=timezone.utc)

    def raising_run(**kw):
        env["calls"].append(kw)
        kw["out"].update({"batch": "slack-20261014-093000", "run": {"ok": False, "error": "timed out"}})
        raise ConnectionError("slack down")
    import jobs_slack_intake
    jobs_slack_intake.run = raising_run   # main_env's monkeypatch restores the original at teardown
    assert w.main([]) == 1
    assert env["health"].unmerged() == ["slack-20261014-093000"]
    assert env["health"].gap_nights("2026-10-14") is None             # no success recorded


def test_batch_not_recorded_when_the_run_was_ok_before_the_exception(main_env):
    env = main_env
    env["now"] = datetime(2026, 10, 14, 9, 30, tzinfo=timezone.utc)

    def raising_run(**kw):
        kw["out"].update({"batch": "slack-20261014-093000", "run": {"ok": True}})
        raise ConnectionError("slack down")
    import jobs_slack_intake
    jobs_slack_intake.run = raising_run
    assert w.main([]) == 1 and env["health"].unmerged() == []


# ---- final-review fix wave ----------------------------------------------------------------------

def test_runner_error_string_is_capped_at_200_chars(tmp_dirs, tmp_path):
    """Fix A: error strings are <= 200 chars (global constraint, spec §2)."""
    s = LeadStore()
    slack = FakeSlack([m("1792000000.000100", "https://a.com/job/1")])

    def boom(ids_path, result_path, limit):
        raise RuntimeError("x" * 500)
    out, _ = go(tmp_path, slack, s, boom)
    assert len(out["run"]["error"]) == 200


def test_scorer_log_line_is_capped_at_200_chars(tmp_path):
    class Bad:
        @staticmethod
        def lead_to_view(l):
            return l

        @staticmethod
        def rank(*a, **k):
            raise RuntimeError("z" * 500)
    cfg = tmp_path / "cfg.json"
    cfg.write_text("{}", encoding="utf-8")
    w.scores_for([{"lead_id": "a"}], NOW, bridge=Bad, config_path=cfg)
    log_text = (tmp_path / "si" / "runs.log").read_text(encoding="utf-8")
    assert "z" * 160 in log_text and "z" * 167 not in log_text   # 34-char prefix + 166 = 200


def test_main_exception_note_is_capped_at_200_chars(main_env):
    env = main_env
    env["now"] = datetime(2026, 10, 14, 9, 30, tzinfo=timezone.utc)

    def raising_run(**kw):
        raise ValueError("y" * 500)
    import jobs_slack_intake
    jobs_slack_intake.run = raising_run
    assert w.main([]) == 1
    text = env["slack"].posts[0][1]
    assert "y" * 180 in text and "y" * 189 not in text


def test_alert_post_failure_log_is_capped_at_200_chars(main_env, tmp_path):
    env = main_env
    env["now"] = datetime(2026, 10, 14, 9, 30, tzinfo=timezone.utc)

    def raising_run(**kw):
        raise ValueError("boom")
    import jobs_slack_intake
    jobs_slack_intake.run = raising_run

    def post_fails(text, thread_ts=None):
        raise ConnectionError("q" * 500)
    env["slack"].post = post_fails
    assert w.main([]) == 1
    log_text = (tmp_path / "si" / "runs.log").read_text(encoding="utf-8")
    assert "q" * 150 in log_text and "q" * 200 not in log_text


def _merge_never_ran_runner(store):
    """result.json says the lead was triaged, but Stage 4 never merged it: the lead file is still pending."""
    def runner(ids_path, result_path, limit):
        ids = ids_path.read_text(encoding="utf-8").split()
        res = {"batch_id": "b", "triaged": ids, "failed": [], "carried": [], "airtable_dedupe": "ok",
               "stopped": False}
        result_path.write_text(json.dumps(res), encoding="utf-8")
        return {"ok": True, **res}
    return runner


def test_triaged_but_unmerged_batch_is_not_ok(tmp_dirs, tmp_path):
    """Fix C: a batch triaged but never merged fails the run, so the batch stays unmerged and the reply says
    "retrying", never "queued (over tonight's cap)"."""
    s = LeadStore()
    slack = FakeSlack([m("1792000000.000100", "https://a.com/job/1")])
    out, _ = go(tmp_path, slack, s, _merge_never_ran_runner(s))
    assert out["run"]["ok"] is False and out["run"]["error"] == "batch not merged"
    assert "retrying next run" in slack.posts[0][1] and "over tonight's cap" not in slack.posts[0][1]


def _failed_result_runner(tmp_dirs, with_checkpoint):
    def runner(ids_path, result_path, limit):
        ids = ids_path.read_text(encoding="utf-8").split()
        if with_checkpoint:
            d = tmp_dirs["ckpt"] / "b"
            d.mkdir(parents=True, exist_ok=True)
            for lid in ids:
                (d / f"{lid}.json").write_text(json.dumps({"lead_id": lid, "status": "failed"}), encoding="utf-8")
        res = {"batch_id": "b", "triaged": [], "failed": ids, "carried": [], "airtable_dedupe": "ok",
               "stopped": False}
        result_path.write_text(json.dumps(res), encoding="utf-8")
        return {"ok": True, **res}
    return runner


def test_failed_lead_with_a_checkpoint_left_pending_fails_the_run(tmp_dirs, tmp_path):
    s = LeadStore()
    slack = FakeSlack([m("1792000000.000100", "https://a.com/job/1")])
    out, _ = go(tmp_path, slack, s, _failed_result_runner(tmp_dirs, with_checkpoint=True))
    assert out["run"]["ok"] is False and out["run"]["error"] == "batch not merged"


def test_failed_lead_without_a_checkpoint_does_not_fail_the_run(tmp_dirs, tmp_path):
    """Fix C boundary: fleet_merge only reads checkpoint files, so a manifest-failed lead with no checkpoint stays
    pending even after a real merge - that is not evidence the merge was skipped."""
    s = LeadStore()
    slack = FakeSlack([m("1792000000.000100", "https://a.com/job/1")])
    out, _ = go(tmp_path, slack, s, _failed_result_runner(tmp_dirs, with_checkpoint=False))
    assert out["run"]["ok"] is True


def _fail(store, lid, when="2026-10-14T11:40:00+00:00", err="timeout"):
    store.update(lid, "triage", {"triage_status": "failed", "triage_error": err, "triaged_at": when,
                                 "company": "Acme", "role_title": "Lead"})


def _failed_lead_from_last_night(tmp_path, s):
    first = FakeSlack([m("1792000000.000100", "https://a.com/job/1")])
    go(tmp_path, first, s, triaging_runner(s, take=0), now=datetime(2026, 10, 14, 11, 30, tzinfo=timezone.utc))
    lid = s.list()[0]["lead_id"]
    _fail(s, lid)
    return lid


def test_redropping_a_failed_link_retries_it(tmp_dirs, tmp_path):
    """Fix D: a re-dropped triage-failed link is queued again (once) and reported from its new triage."""
    s = LeadStore()
    lid = _failed_lead_from_last_night(tmp_path, s)
    slack = FakeSlack([m("1792000000.000100", "https://a.com/job/1"), m("1792000100.000100", "https://a.com/job/1")])
    runner = triaging_runner(s, take=20)
    go(tmp_path, slack, s, runner)
    assert runner.calls == [[lid]]
    assert slack.posts[-1][0] == "1792000100.000100" and "✅ Acme · Lead" in slack.posts[-1][1]


def test_redropped_failed_link_reads_pending_until_triaged(tmp_dirs, tmp_path):
    """A re-drop retry still waiting (over tonight's cap) reads pending, and runs the next night."""
    s = LeadStore()
    lid = _failed_lead_from_last_night(tmp_path, s)
    slack = FakeSlack([m("1792000100.000100", "https://a.com/job/1")])
    led = si.Ledger(tmp_path / si.LEDGER_NAME)
    _HOLDER["fn"] = fake_ingest(s)
    w.run(slack=slack, store=s, ledger=led, user_id=U, channel="CJ", now=NOW, runner=triaging_runner(s, take=0),
          scorer=lambda leads, now: {}, sleep=lambda x: None, run_dir=tmp_path / "run", clock=lambda: NOW, limit=0)
    text = slack.posts[0][1]
    assert "queued (over tonight's cap)" in text and "⚠️" not in text and si.FINAL_TOKEN not in text
    runner = triaging_runner(s, take=20)                              # the retry is carried, not dropped
    go(tmp_path, slack, s, runner, now=datetime(2026, 10, 16, 11, 30, tzinfo=timezone.utc))
    assert runner.calls == [[lid]] and si.FINAL_TOKEN in slack.posts[-1][1]


def test_a_retry_queued_into_a_failed_headless_run_is_refunded(tmp_dirs, tmp_path):
    """Controller ruling on concern 1: when the headless run as a whole fails, every retry queued into it is refunded,
    so the lead keeps its retry and its line stays non-final; the next night retries it."""
    s = LeadStore()
    lid = _failed_lead_from_last_night(tmp_path, s)
    slack = FakeSlack([m("1792000100.000100", "https://a.com/job/1")])
    for fail in ({"ok": False, "error": "timed out"}, RuntimeError("claude missing")):
        def runner(ids_path, result_path, limit, fail=fail):
            if isinstance(fail, Exception):
                raise fail
            return fail
        led = go(tmp_path, slack, s, runner)[1]
        att = led.attempts()
        assert att["auto"] == set() and att["redrop"] == set()
    assert len(slack.posts) == 1
    text = slack.posts[0][1]
    assert si.FINAL_TOKEN not in text and "Could not parse" not in text and "⏳" in text
    runner = triaging_runner(s, take=20)
    go(tmp_path, slack, s, runner, now=datetime(2026, 10, 16, 11, 30, tzinfo=timezone.utc))
    assert runner.calls == [[lid]] and si.FINAL_TOKEN in slack.posts[-1][1] and "✅" in slack.posts[-1][1]


def test_a_refunded_retry_is_spent_by_the_next_real_attempt(tmp_path):
    led = si.Ledger(tmp_path / si.LEDGER_NAME)
    row = {"ts": "1.0", "channel": "C", "state": "retry", "kind": "auto", "lead_id": "a", "batch": "b1"}
    led.append(row)
    led.append({**row, "state": "refund"})
    assert led.attempts()["auto"] == set()
    assert led.append({**row, "batch": "b2"}) is True          # the retry can be queued again
    assert led.attempts()["auto"] == {"a"}


def test_a_retry_that_fails_again_is_final_and_not_requeued(tmp_dirs, tmp_path):
    s = LeadStore()
    lid = _failed_lead_from_last_night(tmp_path, s)
    old = FakeSlack([m("1792000000.000100", "https://a.com/job/1"), m("1792000100.000100", "https://a.com/job/1")])
    calls = []

    def fails_again(ids_path, result_path, limit):
        ids = ids_path.read_text(encoding="utf-8").split()
        calls.append(ids)
        for x in ids:
            _fail(s, x, when=(NOW + timedelta(minutes=30)).isoformat(), err="login wall")
        res = {"batch_id": "b", "triaged": [], "failed": ids, "carried": [], "airtable_dedupe": "ok",
               "stopped": False}
        result_path.write_text(json.dumps(res), encoding="utf-8")
        return {"ok": True, **res}
    go(tmp_path, old, s, fails_again)
    assert calls == [[lid]]
    text = old.posts[-1][1]
    assert "Could not parse job description (login wall)" in text and si.FINAL_TOKEN in text
    go(tmp_path, old, s, fails_again)                                # same-night re-run: no second retry
    go(tmp_path, old, s, fails_again, now=datetime(2026, 10, 16, 11, 30, tzinfo=timezone.utc))
    assert calls == [[lid]]


def test_screened_or_same_role_pending_leads_are_not_queued(tmp_dirs, tmp_path):
    """Fix F: a pending lead already screened out or matched to Airtable never launches a run."""
    s = LeadStore()
    slack = FakeSlack([m("1792000000.000100", "https://a.com/job/1 https://a.com/job/2")])
    go(tmp_path, slack, s, lambda ids_path, result_path, limit: {"ok": False, "error": "timed out"},
       now=datetime(2026, 10, 14, 11, 30, tzinfo=timezone.utc))
    a, b = sorted(l["lead_id"] for l in s.list())
    s.update(a, "intake", {"screen": {"label": "event", "reason": "summit"}})
    s.update(b, "triage", {"airtable_match": {"relation": "same_role"}})
    runner = triaging_runner(s, take=20)
    out, _ = go(tmp_path, slack, s, runner)
    assert runner.calls == [] and out["queued"] == 0


def test_final_reply_allowed_after_a_same_night_partial(tmp_dirs, tmp_path):
    """Fix G: a same-night re-run that finishes the carried leads posts the final reply; never a second partial."""
    s = LeadStore()
    ts = "1792000000.000100"
    slack = FakeSlack([m(ts, "https://a.com/job/1 https://a.com/job/2")])
    go(tmp_path, slack, s, triaging_runner(s, take=1))
    assert len(slack.posts) == 1 and si.FINAL_TOKEN not in slack.posts[0][1]
    go(tmp_path, slack, s, triaging_runner(s, take=0))               # still pending: no second partial
    assert len(slack.posts) == 1
    out, led = go(tmp_path, slack, s, triaging_runner(s, take=20))
    assert len(slack.posts) == 2 and si.FINAL_TOKEN in slack.posts[1][1] and led.state()[ts]["final"]
    go(tmp_path, slack, s, triaging_runner(s, take=20))              # never a second final
    assert len(slack.posts) == 2


def test_no_two_night_alert_when_tonight_succeeds(main_env):
    """Fix H: the "had not run successfully" alert needs tonight to fail too (spec §6, two nights in a row)."""
    env = main_env
    env["health"].success("2026-10-10")
    env["now"] = datetime(2026, 10, 15, 9, 30, tzinfo=timezone.utc)   # PC was off for days; tonight is fine
    env["results"].append(_summary(True))
    assert w.main([]) == 0 and env["slack"].posts == []


def test_headless_command_blocks_write_capable_tools(tmp_path):
    """Fix E: the Gmail and Calendar servers and every Airtable write tool are disallowed in the headless run."""
    seen = {}

    def fake_run(cmd, **kw):
        seen["cmd"] = cmd
        raise w.subprocess.TimeoutExpired(cmd, 1)
    w.run_headless(tmp_path / "ids.txt", tmp_path / "result.json", 20, run=fake_run)
    cmd = seen["cmd"]
    dis = cmd[cmd.index("--disallowed-tools") + 1].split(",")
    for t in ("mcp__claude_ai_Gmail", "mcp__claude_ai_Google_Calendar",
              "mcp__claude_ai_Airtable__create_records_for_table", "mcp__claude_ai_Airtable__update_records_for_table",
              "mcp__claude_ai_Airtable__delete_records_for_table", "mcp__claude_ai_Airtable__create_field",
              "mcp__claude_ai_Airtable__update_field", "Bash(git push:*)", "Bash(git commit:*)"):
        assert t in dis, t
    allowed = cmd[cmd.index("--allowed-tools") + 1].split(",")
    assert not [t for t in allowed if t.startswith(("mcp__claude_ai_Gmail", "mcp__claude_ai_Google_Calendar"))]
    assert not any("curl" in t for t in allowed)


# ---- addendum item 1: Workday JDs prefetched by Python ------------------------------------------------

WD = "https://acme.wd5.myworkdayjobs.com/en-US/External/job/NYC/Head-of-Strategy_JR1"


def test_workday_jd_is_prefetched_into_the_batch_dir_before_the_headless_run(tmp_dirs, tmp_path):
    s = LeadStore()
    slack = FakeSlack([m("1792000000.000100", f"{WD} https://a.com/job/2")])
    fetched, seen_at_run = [], {}

    def fake_fetch(url, log=None):
        fetched.append(url)
        return "Head of Strategy\n\nOwn the roadmap.\n"
    inner = triaging_runner(s, take=20)

    def runner(ids_path, result_path, limit):
        jd = tmp_dirs["ckpt"] / "slack-run" / "jd"
        seen_at_run.update({p.name: p.read_text(encoding="utf-8") for p in jd.glob("*.txt")})
        return inner(ids_path, result_path, limit)
    led = si.Ledger(tmp_path / si.LEDGER_NAME)
    _HOLDER["fn"] = fake_ingest(s)
    w.run(slack=slack, store=s, ledger=led, user_id=U, channel="CJ", now=NOW, runner=runner,
          scorer=lambda leads, now: {}, sleep=lambda x: None, run_dir=tmp_path / "run", clock=lambda: NOW,
          jd_fetch=fake_fetch)
    wd_lid = led.state()["1792000000.000100"]["by_url"][WD]["lead_ids"][0]
    assert fetched == [WD]                                           # only the Workday lead is fetched
    assert seen_at_run == {f"{wd_lid}.txt": "Head of Strategy\n\nOwn the roadmap.\n"}


def test_workday_prefetch_failure_writes_nothing_and_never_blocks(tmp_dirs, tmp_path):
    s = LeadStore()
    slack = FakeSlack([m("1792000000.000100", WD)])
    runner = triaging_runner(s, take=20)
    led = si.Ledger(tmp_path / si.LEDGER_NAME)
    _HOLDER["fn"] = fake_ingest(s)

    def boom(url, log=None):
        raise RuntimeError("should never escape")
    w.run(slack=slack, store=s, ledger=led, user_id=U, channel="CJ", now=NOW, runner=runner,
          scorer=lambda leads, now: {}, sleep=lambda x: None, run_dir=tmp_path / "run", clock=lambda: NOW,
          jd_fetch=boom)
    assert len(runner.calls) == 1 and len(slack.posts) == 1
    assert not list((tmp_dirs["ckpt"] / "slack-run" / "jd").glob("*.txt"))


# ---- addendum item 2: failed-scrape reply and the retry cap -------------------------------------------

REDROP = "Re-drop with a cleaner link or run directly via /job-apply."
N2 = datetime(2026, 10, 16, 11, 30, tzinfo=timezone.utc)
N3 = datetime(2026, 10, 17, 11, 30, tzinfo=timezone.utc)


def failing_runner(store, ledger_path, touch=True):
    """Every queued lead fails triage (touch=False: the subagent died, the lead file is not updated)."""
    calls = []

    def runner(ids_path, result_path, limit):
        ids = ids_path.read_text(encoding="utf-8").split()
        att = si.Ledger(ledger_path).attempts()
        calls.append((ids, set(att["auto"])))
        for lid in ids if touch else []:
            _fail(store, lid, when=datetime.now(timezone.utc).isoformat(), err="fetch_failed")
        res = {"batch_id": "b", "triaged": [], "failed": ids, "carried": [], "airtable_dedupe": "ok",
               "stopped": False}
        result_path.write_text(json.dumps(res), encoding="utf-8")
        return {"ok": True, **res}
    runner.calls = calls
    return runner


def _fail_on_first_night(tmp_path, s, ts="1792000000.000100"):
    slack = FakeSlack([m(ts, "https://a.com/job/1")])

    def first(ids_path, result_path, limit):
        ids = ids_path.read_text(encoding="utf-8").split()
        for lid in ids:
            _fail(s, lid, when=(NOW + timedelta(minutes=20)).isoformat(), err="fetch_failed")
        res = {"batch_id": "b", "triaged": [], "failed": ids, "carried": [], "airtable_dedupe": "ok", "stopped": False}
        result_path.write_text(json.dumps(res), encoding="utf-8")
        return {"ok": True, **res}
    go(tmp_path, slack, s, first)
    return slack, s.list()[0]["lead_id"]


def test_a_failed_scrape_is_retried_once_automatically_then_goes_final(tmp_dirs, tmp_path):
    s = LeadStore()
    slack, lid = _fail_on_first_night(tmp_path, s)
    text = slack.posts[0][1]
    assert "couldn't read it tonight (fetch_failed); retrying once tomorrow night" in text
    assert si.FINAL_TOKEN not in text
    runner = failing_runner(s, tmp_path / si.LEDGER_NAME)
    go(tmp_path, slack, s, runner, now=N2)
    assert runner.calls == [([lid], {lid})]            # queued once, attempt in the ledger before the run started
    final = slack.posts[1][1]
    assert si.FINAL_TOKEN in final and f"Could not parse job description (fetch_failed). {REDROP}" in final
    assert final.splitlines()[1].startswith("⚠️ Acme · Lead — ")
    go(tmp_path, slack, s, runner, now=N3)
    assert len(runner.calls) == 1 and len(slack.posts) == 2


def test_a_retry_whose_subagent_dies_still_spends_its_attempt(tmp_dirs, tmp_path):
    """Controller ruling: attempts count when queued, so a retry with no checkpoint cannot loop."""
    s = LeadStore()
    slack, lid = _fail_on_first_night(tmp_path, s)
    runner = failing_runner(s, tmp_path / si.LEDGER_NAME, touch=False)
    go(tmp_path, slack, s, runner, now=N2)
    assert len(runner.calls) == 1 and si.FINAL_TOKEN in slack.posts[1][1] and REDROP in slack.posts[1][1]
    go(tmp_path, slack, s, runner, now=N3)
    assert len(runner.calls) == 1


def test_an_over_cap_retry_is_not_spent(tmp_dirs, tmp_path):
    """Only the leads inside tonight's cap are queued for real; a retry carried past the cap keeps its attempt."""
    s = LeadStore()
    slack = FakeSlack([m("1792000000.000100", "https://a.com/job/0"), m("1792000000.000200", "https://a.com/job/1")])

    def first(ids_path, result_path, limit):   # night 1: job/0 stays pending (over the cap), job/1 fails
        ids = ids_path.read_text(encoding="utf-8").split()
        _fail(s, ids[1], when=(NOW + timedelta(minutes=20)).isoformat(), err="fetch_failed")
        res = {"batch_id": "b", "triaged": [], "failed": [ids[1]], "carried": [ids[0]], "airtable_dedupe": "ok",
               "stopped": False}
        result_path.write_text(json.dumps(res), encoding="utf-8")
        return {"ok": True, **res}
    go(tmp_path, slack, s, first)
    led = si.Ledger(tmp_path / si.LEDGER_NAME)
    runner = triaging_runner(s, take=1)
    w.run(slack=slack, store=s, ledger=led, user_id=U, channel="CJ", now=N2, runner=runner,
          scorer=lambda leads, now: {}, sleep=lambda x: None, run_dir=tmp_path / "run2", clock=lambda: N2, limit=1)
    assert len(runner.calls[0]) == 2 and led.attempts()["auto"] == set()


# ---- addendum item 3: run-status heartbeat and the silent-failure guard ---------------------------------

def _status(tmp_path):
    return json.loads((tmp_path / "si" / "run-status.json").read_text(encoding="utf-8"))


def test_heartbeat_is_written_before_slack_json_is_read(main_env, tmp_path, monkeypatch):
    env = main_env
    env["now"] = datetime(2026, 10, 14, 9, 30, tzinfo=timezone.utc)
    seen = {}

    def load_cfg():
        seen.update(_status(tmp_path))
        raise OSError("slack.json unreadable")
    monkeypatch.setattr(w, "_load_slack_config", load_cfg)
    assert w.main([]) == 1
    assert seen["state"] == "running" and seen["pid"] == w.os.getpid() and seen["night"] == "2026-10-14"
    assert seen["started"].startswith("2026-10-14T09:30")
    st = _status(tmp_path)
    assert st["state"] == "failed" and st["rc"] == 1 and st["started"] == seen["started"] and st["finished"]
    assert "OSError" in st["notes"][0]
    assert env["slack"].posts == []                               # channel unknown: nothing to alert to
    assert "OSError" in (tmp_path / "si" / "runs.log").read_text(encoding="utf-8")


def test_a_crash_outside_run_alerts_once_per_night_and_records_failed(main_env, tmp_path, monkeypatch):
    env = main_env
    env["now"] = datetime(2026, 10, 14, 9, 30, tzinfo=timezone.utc)
    monkeypatch.setattr(w, "_token", lambda: "xoxb-test")

    def bad_lock(*a, **k):
        raise PermissionError("lock dir not writable")
    monkeypatch.setattr(w.si, "acquire_lock", bad_lock)
    assert w.main([]) == 1
    assert len(env["slack"].posts) == 1
    thread, text = env["slack"].posts[0]
    assert thread is None and "PermissionError" in text and "Your links are safe" in text
    assert _status(tmp_path)["state"] == "failed"
    assert w.main([]) == 1 and len(env["slack"].posts) == 1        # one alert per night


def test_crash_alert_needs_a_token_and_never_raises(main_env, tmp_path, monkeypatch):
    env = main_env
    env["now"] = datetime(2026, 10, 14, 9, 30, tzinfo=timezone.utc)

    def bad_lock(*a, **k):
        raise PermissionError("nope")
    monkeypatch.setattr(w.si, "acquire_lock", bad_lock)
    assert w.main([]) == 1 and env["slack"].posts == []           # _token() is None in main_env
    monkeypatch.setattr(w, "_token", lambda: "xoxb-test")

    def post_fails(text, thread_ts=None):
        raise ConnectionError("slack down")
    env["slack"].post = post_fails
    assert w.main([]) == 1
    assert "crash alert not posted" in (tmp_path / "si" / "runs.log").read_text(encoding="utf-8")


def test_ok_run_records_ok_and_a_busy_lock_records_skipped(main_env, tmp_path):
    env = main_env
    env["now"] = datetime(2026, 10, 14, 9, 30, tzinfo=timezone.utc)
    env["results"].append(_summary(True))
    assert w.main([]) == 0
    st = _status(tmp_path)
    assert st["state"] == "ok" and st["rc"] == 0 and st["notes"] == [] and st["finished"]
    si.acquire_lock(tmp_path / "si" / "intake.lock", env["now"], w.STALE_LOCK_S)   # another run is going
    assert w.main([]) == 0 and _status(tmp_path)["state"] == "ok"          # review fix 7: record left as it was


def test_previous_run_that_never_finished_is_reported(main_env, tmp_path):
    env = main_env
    env["now"] = datetime(2026, 10, 14, 13, 30, tzinfo=timezone.utc)
    (tmp_path / "si").mkdir(exist_ok=True)
    (tmp_path / "si" / "run-status.json").write_text(json.dumps(
        {"state": "running", "started": "2026-10-14T09:30:00+00:00", "pid": 1, "night": "2026-10-14"}),
        encoding="utf-8")
    si.acquire_lock(tmp_path / "si" / "intake.lock", datetime(2026, 10, 14, 9, 30, tzinfo=timezone.utc), 0)
    env["health"].success("2026-10-13")
    env["results"].append(_summary(True))
    assert w.main([]) == 0
    text = env["slack"].posts[0][1]
    assert "the previous run (started 2026-10-14 05:30 America/New_York) never finished: crash or power loss" in text
    assert "lock was older than 3 h" not in text                    # one note for one crash
    assert "never finished" in _status(tmp_path)["notes"][0]


def test_a_recent_running_status_is_not_called_a_crash(main_env, tmp_path):
    env = main_env
    env["now"] = datetime(2026, 10, 14, 10, 30, tzinfo=timezone.utc)
    (tmp_path / "si").mkdir(exist_ok=True)
    (tmp_path / "si" / "run-status.json").write_text(json.dumps(
        {"state": "running", "started": "2026-10-14T09:30:00+00:00", "pid": 1}), encoding="utf-8")
    env["health"].success("2026-10-13")
    env["results"].append(_summary(True))
    assert w.main([]) == 0 and env["slack"].posts == []


def test_a_system_that_never_succeeded_alerts_when_tonight_fails(main_env):
    env = main_env
    env["now"] = datetime(2026, 10, 14, 9, 30, tzinfo=timezone.utc)
    env["results"].append(_summary(False))
    assert w.main([]) == 0
    assert len(env["slack"].posts) == 1 and "has not completed a successful run yet" in env["slack"].posts[0][1]


# ---- review fix wave ----------------------------------------------------------------------------------

def test_an_exception_after_retry_rows_are_written_refunds_them(tmp_dirs, tmp_path, monkeypatch):
    """Review 1a: the refund sits in a finally, so a crash between queueing and the ok check still refunds."""
    s = LeadStore()
    slack, lid = _fail_on_first_night(tmp_path, s)

    def boom(*a, **k):
        raise RuntimeError("store exploded")
    monkeypatch.setattr(w, "_unmerged", boom)
    with pytest.raises(RuntimeError):
        go(tmp_path, slack, s, triaging_runner(s, take=20), now=N2)
    assert si.Ledger(tmp_path / si.LEDGER_NAME).attempts()["auto"] == set()


def test_a_killed_run_is_refunded_at_the_next_start_and_the_lead_is_retried(tmp_dirs, tmp_path, main_env, monkeypatch):
    """Review 1b: the process died (no finally) after writing its retry rows. The next start sees the dead `running`
    status, refunds that batch (no ok result.json), and the lead is retried, not reported final."""
    fake_run = w.run
    monkeypatch.setattr(w, "run", REAL_RUN)
    s = LeadStore()
    slack, lid = _fail_on_first_night(tmp_path, s)
    led = si.Ledger(tmp_path / si.LEDGER_NAME)
    monkeypatch.setattr(w.paths, "leads_dir", lambda: tmp_path)          # main() uses the same ledger file
    led.append({"ts": "1792000000.000100", "channel": "CJ", "state": "retry", "kind": "auto", "lead_id": lid,
                "night": "2026-10-16", "batch": "slack-20261016-093000", "at": "2026-10-16T09:30:00+00:00"})
    (tmp_path / "si" / "20261016-093000").mkdir(parents=True)              # run dir, no result.json: killed
    (tmp_path / "si" / "run-status.json").write_text(json.dumps(
        {"state": "running", "started": "2026-10-16T09:30:00+00:00", "pid": 1}), encoding="utf-8")
    env = main_env
    env["now"] = datetime(2026, 10, 17, 9, 30, tzinfo=timezone.utc)
    env["results"].append(_summary(True, skipped=True))
    monkeypatch.setattr(w, "run", fake_run)
    w.main([])
    assert led.attempts()["auto"] == set()
    monkeypatch.setattr(w, "run", REAL_RUN)
    runner = failing_runner(s, tmp_path / si.LEDGER_NAME)
    go(tmp_path, slack, s, runner, now=N3)
    assert runner.calls and runner.calls[0][0] == [lid]


def test_an_unmerged_batch_with_an_ok_result_is_not_refunded(tmp_dirs, tmp_path, main_env, monkeypatch):
    led = si.Ledger(tmp_path / si.LEDGER_NAME)
    monkeypatch.setattr(w.paths, "leads_dir", lambda: tmp_path)
    led.append({"ts": "1.0", "channel": "CJ", "state": "retry", "kind": "auto", "lead_id": "a",
                "night": "2026-10-16", "batch": "slack-ok", "at": "x"})
    led.append({"ts": "1.0", "channel": "CJ", "state": "retry", "kind": "auto", "lead_id": "b",
                "night": "2026-10-16", "batch": "slack-dead", "at": "x"})
    (tmp_path / "si" / "ok").mkdir(parents=True)
    (tmp_path / "si" / "ok" / "result.json").write_text(json.dumps({"stopped": False}), encoding="utf-8")
    env = main_env
    env["health"].add_unmerged("slack-ok")
    env["health"].add_unmerged("slack-dead")
    env["now"] = datetime(2026, 10, 17, 9, 30, tzinfo=timezone.utc)
    env["results"].append(_summary(True, skipped=True))
    w.main([])
    assert led.attempts()["auto"] == {"a"}


def test_build_queue_auto_retry_uses_the_run_start_not_the_merge_date(tmp_dirs):
    """Review 2: a failure merged after midnight ET by a catch-up run is still retried by the next run."""
    s = LeadStore()
    lid, _ = s.create_or_merge("https://a.com/job/1", {"url": "https://a.com/job/1", "kind": "posting",
                                                         "poster_name": None, "poster_url": None, "added_via": "slack",
                                                         "roundup_of": None, "hint": None})
    s.update(lid, "triage", {"triage_status": "failed", "triaged_at": "2026-10-16T04:30:00+00:00"})  # 00:30 ET
    e = {"status": "ok", "lead_ids": [lid], "roundup": False}
    state = {"1.0": {"by_url": {"u": e}, "pre_triaged": [], "pre_failed": {}, "ingested_night": "2026-10-15"}}
    start = datetime(2026, 10, 16, 9, 30, tzinfo=timezone.utc)                 # same ET date, later run
    assert si.build_queue([m("1.0", "")], state, s, "2026-10-16", started=start) == [lid]
    early = datetime(2026, 10, 16, 4, 0, tzinfo=timezone.utc)                  # the run that failed it
    assert si.build_queue([m("1.0", "")], state, s, "2026-10-16", started=early) == []


def test_unmerged_flags_a_retried_lead_whose_new_checkpoint_never_merged(tmp_dirs, tmp_path):
    """Review 3: a retried lead is already `failed` from an earlier batch; its new checkpoint must be merged."""
    s = LeadStore()
    lid, _ = s.create_or_merge("https://a.com/job/1", {"url": "https://a.com/job/1", "kind": "posting",
                                                         "poster_name": None, "poster_url": None, "added_via": "slack",
                                                         "roundup_of": None, "hint": None})
    s.update(lid, "triage", {"triage_status": "failed", "batch_id": "slack-old"})
    d = tmp_dirs["ckpt"] / "slack-new"
    d.mkdir()
    (d / f"{lid}.json").write_text(json.dumps({"lead_id": lid, "status": "failed"}), encoding="utf-8")
    res = {"batch_id": "slack-new", "triaged": [], "failed": [lid]}
    assert w._unmerged(s, res, "slack-new") == [lid]
    s.update(lid, "triage", {"batch_id": "slack-new"})
    assert w._unmerged(s, res, "slack-new") == []


def test_systemexit_inside_the_run_is_a_crash(main_env, tmp_path, monkeypatch):
    """Review 6: only argparse's SystemExit is an argument exit; one from the run is failed + alert."""
    env = main_env
    env["now"] = datetime(2026, 10, 14, 9, 30, tzinfo=timezone.utc)
    monkeypatch.setattr(w, "_token", lambda: "xoxb-test")

    def exits(**kw):
        raise SystemExit(3)
    monkeypatch.setattr(w, "run", exits)
    assert w.main([]) == 1
    st = _status(tmp_path)
    assert st["state"] == "failed" and "SystemExit" in st["notes"][0] and len(env["slack"].posts) == 1
    with pytest.raises(SystemExit):
        w.main(["--bogus"])
    assert _status(tmp_path)["state"] == "failed" and _status(tmp_path)["rc"] == 2


def test_a_busy_lock_leaves_the_live_running_status_alone(main_env, tmp_path):
    """Review 7: the skip is logged only; the running run's record is left as it was."""
    env = main_env
    env["now"] = datetime(2026, 10, 14, 10, 0, tzinfo=timezone.utc)
    live = {"state": "running", "started": "2026-10-14T09:30:00+00:00", "pid": 4242, "night": "2026-10-14"}
    (tmp_path / "si").mkdir(exist_ok=True)
    (tmp_path / "si" / "run-status.json").write_text(json.dumps(live), encoding="utf-8")
    si.acquire_lock(tmp_path / "si" / "intake.lock", datetime(2026, 10, 14, 9, 30, tzinfo=timezone.utc), 3600)
    assert w.main([]) == 0
    assert _status(tmp_path) == live
    assert "skipped: another run holds the lock" in (tmp_path / "si" / "runs.log").read_text(encoding="utf-8")


def test_help_after_a_killed_run_keeps_the_dead_running_record(main_env, tmp_path):
    """Re-review: --help must not erase the record the next start's refund sweep relies on."""
    main_env["now"] = datetime(2026, 10, 14, 10, 0, tzinfo=timezone.utc)
    dead = {"state": "running", "started": "2026-10-13T09:30:00+00:00", "pid": 4242, "night": "2026-10-13"}
    (tmp_path / "si").mkdir(exist_ok=True)
    (tmp_path / "si" / "run-status.json").write_text(json.dumps(dead), encoding="utf-8")
    with pytest.raises(SystemExit):
        w.main(["--help"])
    assert _status(tmp_path) == dead


def test_a_busy_skip_does_not_restore_running_over_a_run_that_finished_meanwhile(main_env, tmp_path, monkeypatch):
    """Re-review: if the live run writes its outcome between our read and our skip, its outcome stays."""
    main_env["now"] = datetime(2026, 10, 14, 10, 0, tzinfo=timezone.utc)
    live = {"state": "running", "started": "2026-10-14T09:30:00+00:00", "pid": 4242, "night": "2026-10-14"}
    done = {**live, "state": "ok", "finished": "2026-10-14T10:00:01+00:00", "rc": 0, "notes": []}
    (tmp_path / "si").mkdir(exist_ok=True)
    (tmp_path / "si" / "run-status.json").write_text(json.dumps(live), encoding="utf-8")

    def busy_after_live_finishes(*a, **k):
        (tmp_path / "si" / "run-status.json").write_text(json.dumps(done), encoding="utf-8")
        return "busy"
    monkeypatch.setattr(si, "acquire_lock", busy_after_live_finishes)
    assert w.main([]) == 0
    assert _status(tmp_path) == done
