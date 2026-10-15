import json
import threading

import pytest

from lib.lead_store import LeadStore, LeadStoreError, GROUPS

SRC = {"url": "https://www.linkedin.com/posts/a-share-1", "kind": "post", "poster_name": "A",
       "poster_url": None, "added_via": "sheet", "roundup_of": None, "hint": None}


def test_create_then_merge_same_url(tmp_dirs):
    s = LeadStore()
    lid, created = s.create_or_merge("https://a.com/job/1", SRC, post_text="hello")
    assert created
    lid2, created2 = s.create_or_merge("https://a.com/job/1", {**SRC, "url": "https://lnkd.in/x"})
    assert lid2 == lid and not created2
    lead = s.read(lid)
    assert [x["url"] for x in lead["intake"]["sources"]] == [SRC["url"], "https://lnkd.in/x"]
    assert lead["intake"]["post_text"] == "hello"


def test_merge_same_source_twice_is_noop(tmp_dirs):
    s = LeadStore()
    lid, _ = s.create_or_merge("https://a.com/job/1", SRC)
    s.create_or_merge("https://a.com/job/1", SRC)
    assert len(s.read(lid)["intake"]["sources"]) == 1


def test_update_rejects_foreign_group_keys(tmp_dirs):
    s = LeadStore()
    lid, _ = s.create_or_merge("https://a.com/job/1", SRC)
    with pytest.raises(LeadStoreError):
        s.update(lid, "triage", {"decision": "applied"})
    with pytest.raises(LeadStoreError):
        s.update(lid, "nope", {"x": 1})


def test_triage_write_leaves_decision_byte_identical(tmp_dirs):
    s = LeadStore()
    lid, _ = s.create_or_merge("https://a.com/job/1", SRC)
    s.update(lid, "decision", {"decision": "skipped", "history": [{"at": "t", "action": "skipped"}]})
    before = json.dumps(s.read(lid)["decision"], sort_keys=True)
    s.update(lid, "triage", {"bucket": "tier1", "company": "X"})
    assert json.dumps(s.read(lid)["decision"], sort_keys=True) == before


def test_effective_bucket(tmp_dirs):
    assert LeadStore.effective_bucket({"triage": {"bucket": "tier1"}, "decision": {}}) == "tier1"
    assert LeadStore.effective_bucket({"triage": {"bucket": "tier1"},
                                       "decision": {"decision": "moved_tier2"}}) == "tier2"
    assert LeadStore.effective_bucket({"triage": {"bucket": "tier2"},
                                       "decision": {"decision": "skipped"}}) == "skip"
    assert LeadStore.effective_bucket({"triage": {"bucket": "low_fit"},
                                       "decision": {"decision": "restored"}}) == "low_fit"


def test_corrupt_file_read_raises_and_list_skips(tmp_dirs):
    s = LeadStore()
    lid, _ = s.create_or_merge("https://a.com/job/1", SRC)
    (tmp_dirs["leads"] / "badbadbadbad.json").write_text("{not json", encoding="utf-8")
    with pytest.raises(LeadStoreError, match="badbadbadbad"):
        s.read("badbadbadbad")
    with pytest.warns(UserWarning):
        leads = s.list()
    assert [l["lead_id"] for l in leads] == [lid]


def test_concurrent_updates_both_land(tmp_dirs):
    s = LeadStore()
    lid, _ = s.create_or_merge("https://a.com/job/1", SRC)

    def t1():
        s.update(lid, "triage", {"company": "X"})

    def t2():
        s.update(lid, "decision", {"decision": "applied"})

    th = [threading.Thread(target=f) for f in (t1, t2)]
    [t.start() for t in th]
    [t.join() for t in th]
    lead = s.read(lid)
    assert lead["triage"]["company"] == "X" and lead["decision"]["decision"] == "applied"


def test_groups_do_not_overlap():
    names = list(GROUPS)
    for i, a in enumerate(names):
        for b in names[i + 1:]:
            assert not (GROUPS[a] & GROUPS[b])


def test_write_retries_permission_error(tmp_dirs, monkeypatch):
    import lib.lead_store as ls
    monkeypatch.setattr(ls, "RETRY_SLEEP_S", 0)
    real, calls = ls.os.replace, {"n": 0}

    def flaky(*a, **k):
        calls["n"] += 1
        if calls["n"] <= 2:
            raise PermissionError("held open")
        return real(*a, **k)
    monkeypatch.setattr(ls.os, "replace", flaky)
    s = LeadStore()
    lid, _ = s.create_or_merge("https://a.com/job/1", SRC)
    assert calls["n"] == 3 and s.read(lid)["lead_id"] == lid


def test_write_gives_up_with_lead_store_error(tmp_dirs, monkeypatch):
    import lib.lead_store as ls
    monkeypatch.setattr(ls, "RETRY_SLEEP_S", 0)

    def boom(*a, **k):
        raise PermissionError("x")
    monkeypatch.setattr(ls.os, "replace", boom)
    with pytest.raises(LeadStoreError):
        LeadStore().create_or_merge("https://a.com/job/1", SRC)


def test_read_retries_then_converts_permission_error(tmp_dirs, monkeypatch):
    import lib.lead_store as ls
    from pathlib import Path
    monkeypatch.setattr(ls, "RETRY_SLEEP_S", 0)
    s = LeadStore()
    lid, _ = s.create_or_merge("https://a.com/job/1", SRC)
    real, calls = Path.read_text, {"n": 0}

    def flaky(self, *a, **k):
        calls["n"] += 1
        if calls["n"] <= 2:
            raise PermissionError("x")
        return real(self, *a, **k)
    monkeypatch.setattr(Path, "read_text", flaky)
    assert s.read(lid)["lead_id"] == lid and calls["n"] == 3

    def boom(self, *a, **k):
        raise PermissionError("x")
    monkeypatch.setattr(Path, "read_text", boom)
    with pytest.raises(LeadStoreError):
        s.read(lid)
    with pytest.warns(UserWarning):
        assert s.list() == []


def test_decision_group_accepts_jobs_tab_keys(tmp_dirs):
    s = LeadStore()
    lid, _ = s.create_or_merge("https://a.com/job/1", SRC)
    s.update(lid, "decision", {"pending": {"action": "skip"}, "job_status": {"state": "running"},
                               "prep_file": "data/prep/a-2026-10-01.md"})
    d = s.read(lid)["decision"]
    assert d["pending"] == {"action": "skip"} and d["job_status"]["state"] == "running"
    with pytest.raises(LeadStoreError):
        s.update(lid, "triage", {"pending": None})


def test_effective_bucket_moved_tier1():
    assert LeadStore.effective_bucket({"triage": {"bucket": "low_fit"},
                                       "decision": {"decision": "moved_tier1"}}) == "tier1"


def test_delete_removes_file_and_is_idempotent(tmp_dirs):
    s = LeadStore()
    lid, _ = s.create_or_merge("https://a.com/job/1", SRC)
    assert s.delete(lid) is True
    assert not (tmp_dirs["leads"] / f"{lid}.json").exists()
    assert not (tmp_dirs["leads"] / f"{lid}.json.lock").exists()
    assert s.delete(lid) is False
    assert s.list() == []


def test_delete_retries_permission_error(tmp_dirs, monkeypatch):
    import lib.lead_store as ls
    from pathlib import Path
    monkeypatch.setattr(ls, "RETRY_SLEEP_S", 0)
    s = LeadStore()
    lid, _ = s.create_or_merge("https://a.com/job/1", SRC)
    real, calls = Path.unlink, {"n": 0}

    def flaky(self, *a, **k):
        if self.name == f"{lid}.json":
            calls["n"] += 1
            if calls["n"] <= 2:
                raise PermissionError("held open")
        return real(self, *a, **k)
    monkeypatch.setattr(Path, "unlink", flaky)
    assert s.delete(lid) is True and calls["n"] == 3


def test_delete_gives_up_with_lead_store_error(tmp_dirs, monkeypatch):
    import lib.lead_store as ls
    from pathlib import Path
    monkeypatch.setattr(ls, "RETRY_SLEEP_S", 0)
    s = LeadStore()
    lid, _ = s.create_or_merge("https://a.com/job/1", SRC)
    real = Path.unlink

    def boom(self, *a, **k):
        if self.name == f"{lid}.json":
            raise PermissionError("x")
        return real(self, *a, **k)
    monkeypatch.setattr(Path, "unlink", boom)
    with pytest.raises(LeadStoreError):
        s.delete(lid)
    assert not (tmp_dirs["leads"] / f"{lid}.json.lock").exists()


def test_skip_log_appends_lines_and_is_not_a_lead(tmp_dirs):
    s = LeadStore()
    lid, _ = s.create_or_merge("https://a.com/job/1", SRC)
    s.append_skip({"lead_id": "aaaaaaaaaaaa", "company": "Société Générale", "reason": "location"})
    s.append_skip({"lead_id": "bbbbbbbbbbbb", "company": "B", "reason": "pay"})
    raw = s.skips_path.read_text(encoding="utf-8")
    assert s.skips_path.name == "_skips.jsonl" and "Société Générale" in raw
    assert [json.loads(x)["lead_id"] for x in raw.splitlines()] == ["aaaaaaaaaaaa", "bbbbbbbbbbbb"]
    assert s.skipped_ids() == {"aaaaaaaaaaaa", "bbbbbbbbbbbb"}
    assert [l["lead_id"] for l in s.list()] == [lid]


def test_skipped_ids_empty_without_log(tmp_dirs):
    assert LeadStore().skipped_ids() == set()


def test_skipped_ids_tolerates_bad_lines(tmp_dirs):
    s = LeadStore()
    s.skips_path.write_text('{"lead_id": "aaaaaaaaaaaa"}\n\nnot json\n[1]\n{"no_id": 1}\n', encoding="utf-8")
    with pytest.warns(UserWarning):
        assert s.skipped_ids() == {"aaaaaaaaaaaa"}


def test_effective_bucket_is_none_for_v2_leads(tmp_dirs):
    from lib.lead_store import LeadStore
    assert LeadStore.effective_bucket({"schema_version": 1, "triage": {"bucket": "tier1"}}) == "tier1"
    assert LeadStore.effective_bucket({"schema_version": 1, "triage": {"bucket": "low_fit"},
                                       "decision": {"decision": "moved_tier1"}}) == "tier1"
    assert LeadStore.effective_bucket({"schema_version": 2, "triage": {"bucket": "tier1"},
                                       "decision": {"decision": "moved_tier1"}}) is None


def test_update_can_set_schema_version_only_with_triage(tmp_dirs):
    import pytest
    from lib.lead_store import LeadStore, LeadStoreError
    s = LeadStore()
    lid, _ = s.create_or_merge("https://a.com/j/1", {"url": "https://a.com/j/1", "kind": "posting"})
    assert s.update(lid, "triage", {"route": "board", "scores": {}}, schema_version=2)["schema_version"] == 2
    with pytest.raises(LeadStoreError):
        s.update(lid, "decision", {"decision": None}, schema_version=2)
