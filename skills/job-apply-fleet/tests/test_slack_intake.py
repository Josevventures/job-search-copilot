import json
from datetime import datetime, timezone

from lib import slack_intake as si
from lib.lead_store import LeadStore

U = "U0TESTUSER1"


def msg(ts, text, **kw):
    return {"ts": ts, "user": U, "text": text, "type": "message", **kw}


def test_is_drop_filters_bots_subtypes_threads_and_other_users():
    assert si.is_drop(msg("1.0", "x"), U)
    assert not si.is_drop(msg("1.0", "x", bot_id="B1"), U)
    assert not si.is_drop(msg("1.0", "x", subtype="channel_join"), U)
    assert not si.is_drop(msg("2.0", "x", thread_ts="1.0"), U)
    assert si.is_drop(msg("1.0", "x", thread_ts="1.0"), U)          # a thread parent is still a drop
    assert not si.is_drop({**msg("1.0", "x"), "user": "U999"}, U)


def test_parse_message_markup_bare_dedupe_and_note():
    text = ("Dana's team, he'll refer me <https://jobs.example.com/1?a=1&amp;b=2|Strategy Lead> "
            "and https://b.com/careers/9. also <https://jobs.example.com/1?a=1&amp;b=2>")
    urls, note = si.parse_message(text)
    assert urls == ["https://jobs.example.com/1?a=1&b=2", "https://b.com/careers/9"]
    assert note == "Dana's team, he'll refer me and also"


def test_parse_message_no_link_and_note_cap():
    assert si.parse_message("just a thought") == ([], "just a thought")
    assert len(si.parse_message("x" * 500 + " https://a.com/j")[1]) == 300


def test_ledger_state_and_append_guard(tmp_path):
    led = si.Ledger(tmp_path / si.LEDGER_NAME)
    row = {"ts": "1.0", "channel": "C", "state": "ingested", "kind": None, "night": "2026-10-15",
           "by_url": {"u": {"status": "ok", "lead_ids": ["a"], "roundup": False}}, "pre_triaged": []}
    assert led.append(row) is True and led.append(row) is False
    led.append({"ts": "1.0", "channel": "C", "state": "replied", "kind": "partial", "night": "2026-10-15",
                "pending": ["a"]})
    st = led.state()["1.0"]
    assert st["ingested"] and st["by_url"]["u"]["lead_ids"] == ["a"] and st["ingested_night"] == "2026-10-15"
    assert st["last_pending"] == ["a"] and st["replied_nights"] == ["2026-10-15"] and not st["final"]
    led.append({"ts": "1.0", "channel": "C", "state": "replied", "kind": "final", "night": "2026-10-16"})
    assert led.state()["1.0"]["final"]


def test_ledger_merges_a_second_ingest_row_for_new_links(tmp_path):
    led = si.Ledger(tmp_path / si.LEDGER_NAME)
    led.append({"ts": "1.0", "state": "ingested", "kind": None, "night": "n1",
                "by_url": {"u1": {"status": "ok", "lead_ids": ["a"], "roundup": False}}, "pre_triaged": []})
    led.append({"ts": "1.0", "state": "ingested", "kind": None, "night": "n2",
                "by_url": {"u2": {"status": "ok", "lead_ids": ["b"], "roundup": False}}, "pre_triaged": ["b"]})
    st = led.state()["1.0"]
    assert set(st["by_url"]) == {"u1", "u2"} and st["pre_triaged"] == ["b"] and st["ingested_night"] == "n2"


def test_ledger_accepts_a_same_night_ingest_row_only_when_it_brings_new_links(tmp_path):
    led = si.Ledger(tmp_path / si.LEDGER_NAME)
    first = {"ts": "1.0", "state": "ingested", "kind": None, "night": "n1",
             "by_url": {"u1": {"status": "ok", "lead_ids": ["a"], "roundup": False}}, "pre_triaged": []}
    added = {"ts": "1.0", "state": "ingested", "kind": None, "night": "n1",
             "by_url": {"u2": {"status": "ok", "lead_ids": ["b"], "roundup": False}}, "pre_triaged": []}
    assert led.append(first) is True and led.append(added) is True
    assert led.append(dict(added)) is False and led.append(dict(first)) is False   # identical link sets refused
    st = led.state()["1.0"]
    assert set(st["by_url"]) == {"u1", "u2"} and st["ingested_night"] == "n1"


def test_ledger_tolerates_a_corrupt_line(tmp_path):
    p = tmp_path / si.LEDGER_NAME
    p.write_text('not json\n{"ts": "1.0", "state": "replied", "kind": "final", "night": "n"}\n', encoding="utf-8")
    assert si.Ledger(p).state()["1.0"]["final"]


def test_work_skips_final_and_sorts_oldest_first():
    msgs = [msg("3.0", "c"), msg("1.0", "a"), msg("2.0", "b", bot_id="B")]
    state = {"1.0": {"final": True}}
    assert [m["ts"] for m in si.work(msgs, state, U)] == ["3.0"]


def test_stale_lists_unfinished_messages_older_than_the_window():
    state = {"1.0": {"final": False}, "5.0": {"final": False}, "0.5": {"final": True}}
    assert si.stale(state, 2.0) == ["1.0"]


def src(u):
    return {"url": u, "kind": "posting", "poster_name": None, "poster_url": None, "added_via": "slack",
            "roundup_of": None, "hint": None}


def test_build_queue_carried_first_dedupes_and_skips_done(tmp_dirs):
    s = LeadStore()
    a, _ = s.create_or_merge("https://a.com/job/1", src("https://a.com/job/1"))
    b, _ = s.create_or_merge("https://a.com/job/2", src("https://a.com/job/2"))
    c, _ = s.create_or_merge("https://a.com/job/3", src("https://a.com/job/3"))
    d, _ = s.create_or_merge("https://a.com/job/4", src("https://a.com/job/4"))
    s.update(d, "triage", {"triage_status": "complete"})
    e = lambda *ids: {"status": "ok", "lead_ids": list(ids), "roundup": False}  # noqa: E731
    state = {
        "1.0": {"by_url": {"u1": e(b)}, "pre_triaged": [], "ingested_night": "2026-10-14"},
        "2.0": {"by_url": {"u2": e(c, a), "u3": e(b)}, "pre_triaged": [], "ingested_night": "2026-10-15"},
        "3.0": {"by_url": {"u4": e(d)}, "pre_triaged": [d], "ingested_night": "2026-10-15"},
    }
    work = [msg("1.0", ""), msg("2.0", ""), msg("3.0", "")]
    assert si.build_queue(work, state, s, "2026-10-15") == [b, c, a]


def test_slack_intake_dir_env_override(monkeypatch, tmp_path):
    from lib import paths
    monkeypatch.setenv("JOB_SLACK_INTAKE_DIR", str(tmp_path))
    assert paths.slack_intake_dir() == tmp_path


from datetime import datetime, timedelta, timezone

UTC = timezone.utc


def test_night_of_uses_et_date():
    assert si.night_of(datetime(2026, 10, 15, 3, 30, tzinfo=UTC)) == "2026-10-14"   # 23:30 ET on the 14th
    assert si.night_of(datetime(2026, 10, 15, 9, 30, tzinfo=UTC)) == "2026-10-15"


def test_quiet_wait_until_7am_et_including_dst():
    # 06:10 EDT (UTC-4) -> 50 min
    assert si.quiet_wait_seconds(datetime(2026, 10, 15, 10, 10, tzinfo=UTC)) == 50 * 60
    # 06:10 EST (UTC-5) in December -> 50 min
    assert si.quiet_wait_seconds(datetime(2026, 12, 15, 11, 10, tzinfo=UTC)) == 50 * 60
    # 23:00 ET -> 8 h
    assert si.quiet_wait_seconds(datetime(2026, 10, 16, 3, 0, tzinfo=UTC)) == 8 * 3600
    # 10:00 ET (late catch-up) -> no wait
    assert si.quiet_wait_seconds(datetime(2026, 10, 15, 14, 0, tzinfo=UTC)) == 0


def test_quiet_wait_ends_at_7am_wall_clock_on_dst_change_nights():
    # Fall back (2026-11-01): 22:30 EDT Oct 31 = 02:30 UTC -> 07:00 EST = 12:00 UTC, 9.5 h of real time
    now = datetime(2026, 11, 1, 2, 30, tzinfo=UTC)
    w = si.quiet_wait_seconds(now)
    assert w == 9.5 * 3600
    end = (now + timedelta(seconds=w)).astimezone(si.ET)
    assert (end.date().isoformat(), end.hour, end.minute) == ("2026-11-01", 7, 0)
    # Spring forward (2027-03-14): 23:00 EST Mar 13 = 04:00 UTC -> 07:00 EDT = 11:00 UTC, 7 h
    now = datetime(2027, 3, 14, 4, 0, tzinfo=UTC)
    w = si.quiet_wait_seconds(now)
    assert w == 7 * 3600
    end = (now + timedelta(seconds=w)).astimezone(si.ET)
    assert (end.date().isoformat(), end.hour, end.minute) == ("2027-03-14", 7, 0)


def test_lock_ok_busy_stale(tmp_path):
    p = tmp_path / "intake.lock"
    t0 = datetime(2026, 10, 15, 9, 30, tzinfo=UTC)
    assert si.acquire_lock(p, t0, 3 * 3600) == "ok"
    assert si.acquire_lock(p, t0 + timedelta(hours=1), 3 * 3600) == "busy"
    assert si.acquire_lock(p, t0 + timedelta(hours=4), 3 * 3600) == "stale"
    si.release_lock(p)
    assert not p.exists()


def test_health_gap_and_once_per_night_alert(tmp_path):
    h = si.Health(tmp_path / "state.json")
    assert h.gap_nights("2026-10-15") is None
    h.success("2026-10-12")
    assert si.Health(tmp_path / "state.json").gap_nights("2026-10-15") == 3
    assert h.can_alert("2026-10-15")
    h.alerted("2026-10-15")
    assert not si.Health(tmp_path / "state.json").can_alert("2026-10-15")


def test_failure_text_mentions_safety_and_stale():
    t = si.failure_text(["RuntimeError: boom"], ["1790000000.000100"])
    assert t.startswith("Jobs intake failed last night: RuntimeError: boom.")
    assert "Your links are safe and will be retried." in t and "1790000000.000100" in t



def test_ledger_state_records_pre_failed_with_the_redrop_time(tmp_path):
    led = si.Ledger(tmp_path / si.LEDGER_NAME)
    led.append({"ts": "1.0", "state": "ingested", "kind": None, "night": "n1", "at": "2026-10-15T09:30:00+00:00",
                "by_url": {"u": {"status": "ok", "lead_ids": ["a"], "roundup": False}}, "pre_triaged": [],
                "pre_failed": ["a"]})
    assert led.state()["1.0"]["pre_failed"] == {"a": "2026-10-15T09:30:00+00:00"}
    assert si.blank_state()["pre_failed"] == {}


def test_retry_due_only_until_the_lead_is_triaged_after_the_redrop():
    at = "2026-10-15T09:30:00+00:00"
    failed = lambda when: {"triage": {"triage_status": "failed", "triaged_at": when}}  # noqa: E731
    assert si.retry_due(failed("2026-10-14T09:40:00+00:00"), at)
    assert si.retry_due({"triage": {"triage_status": "failed"}}, at)
    assert not si.retry_due(failed("2026-10-15T10:00:00+00:00"), at)
    assert not si.retry_due({"triage": {"triage_status": "complete", "triaged_at": "2026-10-14"}}, at)
    assert not si.retry_due(None, at)


def test_build_queue_retries_a_redropped_failed_lead_and_skips_screened_and_same_role(tmp_dirs):
    s = LeadStore()
    ids = [s.create_or_merge(f"https://a.com/job/{i}", src(f"https://a.com/job/{i}"))[0] for i in range(5)]
    f_old, f_redrop, f_done, scr, same = ids
    for lid in (f_old, f_redrop):
        s.update(lid, "triage", {"triage_status": "failed", "triaged_at": "2026-10-14T09:40:00+00:00"})
    s.update(f_done, "triage", {"triage_status": "failed", "triaged_at": "2026-10-15T10:00:00+00:00"})
    s.update(scr, "intake", {"screen": {"label": "event", "reason": "summit"}})
    s.update(same, "triage", {"airtable_match": {"relation": "same_role"}})
    e = lambda *x: {"status": "ok", "lead_ids": list(x), "roundup": False}  # noqa: E731
    at = "2026-10-15T09:30:00+00:00"
    state = {"1.0": {"by_url": {"u": e(f_old, f_redrop, f_done, scr, same)}, "pre_triaged": [],
                     "pre_failed": {f_redrop: at, f_done: at}, "ingested_night": "2026-10-15"}}
    # f_old: its one automatic retry (addendum item 2); f_done failed tonight, after the re-drop: retried tomorrow
    assert si.build_queue([msg("1.0", "")], state, s, "2026-10-15", labels=["event"],
                          started=datetime(2026, 10, 15, 9, 30, tzinfo=timezone.utc)) == [f_old, f_redrop]


# ---- addendum item 2: one automatic retry, attempts counted when queued ----------------------------

def test_ledger_records_retry_attempts_per_lead_and_keeps_them_out_of_message_state(tmp_path):
    led = si.Ledger(tmp_path / si.LEDGER_NAME)
    row = {"ts": "1.0", "channel": "C", "state": "retry", "kind": "auto", "lead_id": "a", "night": "n2", "at": "x"}
    assert led.append(row) is True and led.append(row) is False
    assert led.append({**row, "lead_id": "b"}) is True                       # another lead is another attempt
    led.append({"ts": "2.0", "channel": "C", "state": "retry", "kind": "redrop", "lead_id": "a", "night": "n2"})
    att = led.attempts()
    assert att["auto"] == {"a", "b"} and att["redrop"] == {("2.0", "a")}
    assert led.state() == {}                                                 # retry rows are not message state


def _failed(s, lid, when):
    s.update(lid, "triage", {"triage_status": "failed", "triaged_at": when, "triage_error": "fetch_failed"})


def test_build_queue_auto_retries_a_failure_from_an_earlier_night_once(tmp_dirs):
    s = LeadStore()
    ids = [s.create_or_merge(f"https://a.com/job/{i}", src(f"https://a.com/job/{i}"))[0] for i in range(3)]
    earlier, tonight, spent = ids
    _failed(s, earlier, "2026-10-14T09:40:00+00:00")
    _failed(s, tonight, "2026-10-15T09:40:00+00:00")        # failed tonight: its retry is tomorrow night
    _failed(s, spent, "2026-10-13T09:40:00+00:00")
    e = {"status": "ok", "lead_ids": ids, "roundup": False}
    state = {"1.0": {"by_url": {"u": e}, "pre_triaged": [], "pre_failed": {}, "ingested_night": "2026-10-14"}}
    kinds = {}
    q = si.build_queue([msg("1.0", "")], state, s, "2026-10-15", attempts={"auto": {spent}, "redrop": set()},
                       kinds=kinds, started=datetime(2026, 10, 15, 9, 30, tzinfo=timezone.utc))
    assert q == [earlier] and kinds == {earlier: [("auto", "1.0")]}


def test_build_queue_one_attempt_spends_every_retry_due_for_the_lead(tmp_dirs):
    """A re-drop of a lead whose automatic retry is also due runs once and spends both."""
    s = LeadStore()
    lid, _ = s.create_or_merge("https://a.com/job/1", src("https://a.com/job/1"))
    _failed(s, lid, "2026-10-14T09:40:00+00:00")
    e = {"status": "ok", "lead_ids": [lid], "roundup": False}
    at = "2026-10-15T09:30:00+00:00"
    state = {"1.0": {"by_url": {"u": e}, "pre_triaged": [], "pre_failed": {}, "ingested_night": "2026-10-14"},
             "2.0": {"by_url": {"u": e}, "pre_triaged": [], "pre_failed": {lid: at}, "ingested_night": "2026-10-15"}}
    kinds = {}
    work = [msg("1.0", ""), msg("2.0", "")]
    assert si.build_queue(work, state, s, "2026-10-15", kinds=kinds) == [lid]
    assert sorted(kinds[lid]) == [("auto", "1.0"), ("redrop", "2.0")]
    # both spent: nothing is due any more
    att = {"auto": {lid}, "redrop": {("2.0", lid)}}
    assert si.build_queue(work, state, s, "2026-10-16", attempts=att) == []
