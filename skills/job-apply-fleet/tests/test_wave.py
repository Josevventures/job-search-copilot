import json

from lib.lead_store import LeadStore
from lib.prefs import load
import fleet_wave as wv

P = load()


def src(u):
    return {"url": u, "kind": "posting", "poster_name": None, "poster_url": None, "added_via": "sheet",
            "roundup_of": None, "hint": "Brightfield TPM"}


def seed(n):
    s = LeadStore()
    return s, [s.create_or_merge(f"https://a.com/job/{i}", src(f"https://a.com/job/{i}"),
                                 jd_links=[f"https://a.com/job/{i}"])[0] for i in range(n)]


def test_init_writes_manifest_and_waves(tmp_dirs, capsys):
    s, ids = seed(8)
    assert wv.init("b1", s, False, False, 20, False, P) == 0
    m = json.loads((tmp_dirs["ckpt"] / "b1" / "_manifest.json").read_text(encoding="utf-8"))
    assert set(m["leads"]) == set(ids) and m["concurrency_cap"] == 6
    assert "8 leads" in capsys.readouterr().out
    assert len(wv.dispatch_lines("b1", 1, s)) == 6 and len(wv.dispatch_lines("b1", 2, s)) == 2
    assert "hint=Brightfield TPM" in wv.dispatch_lines("b1", 1, s)[0]


def test_init_refuses_over_max_without_yes(tmp_dirs):
    s, _ = seed(25)
    assert wv.init("b1", s, False, False, 20, False, P) == 3
    assert not (tmp_dirs["ckpt"] / "b1" / "_manifest.json").exists()
    assert wv.init("b1", s, False, False, 20, True, P) == 0


def test_select_excludes_airtable_and_screened_and_complete(tmp_dirs):
    s, ids = seed(4)
    s.update(ids[0], "triage", {"triage_status": "complete", "airtable_match": {"relation": "same_role"}})
    s.update(ids[1], "intake", {"screen": {"label": "non_job", "reason": "x"}})
    s.update(ids[2], "triage", {"triage_status": "complete", "bucket": "tier1"})
    assert [l["lead_id"] for l in wv.select_leads(s, False, False, P)] == [ids[3]]
    assert {l["lead_id"] for l in wv.select_leads(s, True, False, P)} == {ids[2], ids[3]}


def test_only_failed(tmp_dirs):
    s, ids = seed(3)
    s.update(ids[1], "triage", {"triage_status": "failed"})
    assert [l["lead_id"] for l in wv.select_leads(s, False, True, P)] == [ids[1]]


def test_collect_marks_missing_as_failed_and_over_half(tmp_dirs):
    s, ids = seed(2)
    wv.init("b1", s, False, False, 20, False, P)
    wv.mark("b1", 1)
    good = {"url": "u", "lead_id": ids[0], "batch_id": "b1", "status": "failed", "fetch_method": "webfetch",
            "error": "fetch_failed"}
    (tmp_dirs["ckpt"] / "b1" / f"{ids[0]}.json").write_text(json.dumps(good), encoding="utf-8")
    rep = wv.collect("b1", 1)
    assert rep == {"complete": 0, "failed": 2, "total": 2}
    assert wv.over_half_failed("b1")


def test_init_refuses_existing_manifest(tmp_dirs, capsys):
    s, _ = seed(2)
    assert wv.init("b1", s, False, False, 20, False, P) == 0
    mp = tmp_dirs["ckpt"] / "b1" / "_manifest.json"
    before = mp.read_text(encoding="utf-8")
    assert wv.init("b1", s, False, False, 20, False, P) == 2
    assert "already has a manifest" in capsys.readouterr().out
    assert mp.read_text(encoding="utf-8") == before


def test_select_leads_ids_file_limits_the_batch(tmp_dirs):
    from lib.lead_store import LeadStore
    from lib.prefs import load
    import fleet_wave
    s = LeadStore()
    ids = [s.create_or_merge(f"https://a.com/j/{i}", {"url": f"https://a.com/j/{i}", "kind": "posting"})[0]
           for i in range(3)]
    got = fleet_wave.select_leads(s, True, False, load(), ids={ids[0], ids[2]})
    assert sorted(l["lead_id"] for l in got) == sorted([ids[0], ids[2]])


def test_init_reads_ids_file(tmp_dirs, tmp_path):
    from lib.lead_store import LeadStore
    import fleet_wave
    s = LeadStore()
    lid = s.create_or_merge("https://a.com/j/9", {"url": "https://a.com/j/9", "kind": "posting"})[0]
    f = tmp_path / "ids.txt"
    f.write_text(f"# pilot\n{lid}\n\n", encoding="utf-8")
    assert fleet_wave.main(["init", "--batch", "b-ids", "--retriage", "--ids-file", str(f)]) == 0
    assert list(fleet_wave._load("b-ids")["leads"]) == [lid]


def test_dispatch_line_carries_the_newest_note(tmp_dirs):
    s = LeadStore()
    lid, _ = s.create_or_merge("https://a.com/job/1", {**src("https://a.com/job/1"), "note": "Dana's team"},
                               jd_links=["https://a.com/job/1"])
    assert wv.init("bn", s, False, False, 20, False, P) == 0
    assert wv.dispatch_lines("bn", 1, s)[0].endswith("| note=Dana's team")


def test_dispatch_line_without_note_says_none(tmp_dirs):
    s, ids = seed(1)
    wv.init("bnn", s, False, False, 20, False, P)
    assert wv.dispatch_lines("bnn", 1, s)[0].endswith("| note=none")


def test_limit_takes_the_first_n_in_ids_order_and_records_carried(tmp_dirs):
    s, ids = seed(5)
    order = [ids[3], ids[1], ids[4], ids[0], ids[2]]
    assert wv.init("bl", s, False, False, 2, False, P, ids=order, limit=2) == 0   # no exit 3 with --limit
    m = json.loads((tmp_dirs["ckpt"] / "bl" / "_manifest.json").read_text(encoding="utf-8"))
    assert list(m["leads"]) == [ids[3], ids[1]]
    assert m["carried"] == [ids[4], ids[0], ids[2]]


def test_without_limit_over_cap_still_refuses(tmp_dirs):
    s, ids = seed(3)
    assert wv.init("bx", s, False, False, 2, False, P, ids=ids) == 3


def test_ids_file_order_is_preserved(tmp_dirs, tmp_path):
    s, ids = seed(3)
    f = tmp_path / "ids.txt"
    f.write_text("# queue\n" + "\n".join([ids[2], ids[0], ids[1]]) + "\n", encoding="utf-8")
    assert wv.main(["init", "--batch", "bo", "--ids-file", str(f), "--limit", "2"]) == 0
    m = json.loads((tmp_dirs["ckpt"] / "bo" / "_manifest.json").read_text(encoding="utf-8"))
    assert list(m["leads"]) == [ids[2], ids[0]] and m["carried"] == [ids[1]]


def test_result_summarises_the_manifest(tmp_dirs, tmp_path):
    s, ids = seed(3)
    wv.init("br", s, False, False, 2, False, P, ids=ids, limit=2)
    m = json.loads((tmp_dirs["ckpt"] / "br" / "_manifest.json").read_text(encoding="utf-8"))
    m["leads"][ids[0]]["status"], m["leads"][ids[1]]["status"] = "complete", "failed"
    (tmp_dirs["ckpt"] / "br" / "_manifest.json").write_text(json.dumps(m), encoding="utf-8")
    out = tmp_path / "result.json"
    assert wv.main(["result", "--batch", "br", "--out", str(out), "--airtable-dedupe", "skipped"]) == 0
    assert json.loads(out.read_text(encoding="utf-8")) == {
        "batch_id": "br", "triaged": [ids[0]], "failed": [ids[1]], "carried": [ids[2]],
        "airtable_dedupe": "skipped", "stopped": False}


def test_result_requires_out(tmp_dirs):
    """Fix J: `result` without --out is a usage error, not a crash after reading the manifest."""
    import pytest
    with pytest.raises(SystemExit) as e:
        wv.main(["result", "--batch", "br"])
    assert e.value.code == 2
