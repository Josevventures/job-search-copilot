"""Ranking-engine spec §6: the scoring config file and its API."""
import json
import os
import sys
import threading
from datetime import datetime, timezone
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import jobs_scoring_config as jsc  # noqa: E402

GOLDEN = json.loads((Path(__file__).resolve().parents[1] / "tests_js" / "fixtures" / "scoring_golden.json")
                    .read_text(encoding="utf-8"))
W = {"role_fit": 25, "domain": 20, "odds": 20, "comp": 15, "location": 10, "freshness": 10}


@pytest.fixture
def store(tmp_path):
    return jsc.ConfigStore(tmp_path / "jobs_scoring_config.json",
                           clock=lambda: datetime(2026, 10, 13, 16, 0, tzinfo=timezone.utc))


@pytest.fixture
def client(store):
    app = FastAPI()
    app.include_router(jsc.make_router(store))
    return TestClient(app)


def body(**kw):
    b = {"expected_version": 0, "active_preset": None, "active_weights": dict(W),
         "compensation_settings": {"annual_base_floor": 120000}}
    b.update(kw)
    return b


def test_defaults_match_the_spec():
    d = jsc.defaults()
    assert d["compensation_settings"] == {"annual_base_floor": 100000, "hard_gate_clamp_ceiling": 40}
    assert d["active_preset"] == "Balanced" and set(d["presets"]) == {"Balanced", "High Leverage", "Mission First"}
    assert list(d["active_weights"]) == list(jsc.FACTORS) == ["role_fit", "domain", "odds", "comp", "location", "freshness"]
    assert d["presets"]["High Leverage"]["odds"] == 0.40 and d["presets"]["Mission First"]["domain"] == 0.40
    assert d["market_bands"]["senior"] == [120000, 160000] and d["market_bands_note"] == "sample band"
    assert d["home_metros"] == ["boston"]
    assert d["bands"] == {"apply_now_min": 75, "worth_a_look_min": 55}


def test_normalize_agrees_with_the_golden_fixture():
    for c in GOLDEN["normalize_cases"]:
        n = jsc.normalize(c["raw"])
        assert sum(n.values()) == pytest.approx(1.0, abs=1e-9)
        for k in jsc.FACTORS:
            assert n[k] == pytest.approx(c["expected"][k], abs=1e-6)


def test_get_without_file_returns_defaults_and_writes_nothing(client, store):
    r = client.get("/api/jobs/scoring-config").json()
    assert r["config"]["version"] == 0 and r["warning"] is None and r["factors"] == list(jsc.FACTORS)
    assert not store.path.exists()


def test_post_normalizes_bumps_version_and_writes_atomically(client, store):
    r = client.post("/api/jobs/scoring-config", json=body())
    assert r.status_code == 200
    saved = json.loads(store.path.read_text(encoding="utf-8"))
    assert saved["version"] == 1 and saved["updated_at"] == "2026-10-13T16:00:00+00:00"
    assert saved["active_weights"]["role_fit"] == pytest.approx(0.25, abs=1e-12)
    assert saved["compensation_settings"]["annual_base_floor"] == 120000
    assert saved["compensation_settings"]["hard_gate_clamp_ceiling"] == 40
    assert saved["presets"] == jsc.defaults()["presets"]
    assert not list(store.path.parent.glob("*.tmp"))


def test_named_preset_wins_over_sent_weights(client, store):
    client.post("/api/jobs/scoring-config", json=body(active_preset="Mission First"))
    assert json.loads(store.path.read_text(encoding="utf-8"))["active_weights"]["domain"] == 0.40


def test_stale_expected_version_is_409_with_current_doc(client):
    assert client.post("/api/jobs/scoring-config", json=body()).status_code == 200
    r = client.post("/api/jobs/scoring-config", json=body(expected_version=0))
    assert r.status_code == 409 and r.json()["detail"]["config"]["version"] == 1


@pytest.mark.parametrize("b,needle", [
    (body(active_weights={**W, "vibes": 5}), "active_weights"),
    (body(active_weights={k: v for k, v in W.items() if k != "odds"}), "active_weights"),
    (body(active_weights={**W, "odds": -1}), "odds"),
    (body(active_weights={**W, "odds": 1001}), "odds"),
    (body(active_weights={**W, "odds": "high"}), "odds"),
    (body(active_weights={**W, "odds": True}), "odds"),
    (body(compensation_settings={"annual_base_floor": 9999}), "annual_base_floor"),
    (body(compensation_settings={"annual_base_floor": 0}), "annual_base_floor"),
    (body(compensation_settings={"annual_base_floor": -5}), "annual_base_floor"),
    (body(compensation_settings={"annual_base_floor": 120000.5}), "annual_base_floor"),
    (body(compensation_settings={"annual_base_floor": 120000, "hard_gate_clamp_ceiling": 0}), "hard_gate_clamp_ceiling"),
    (body(compensation_settings=None), "compensation_settings"),
    (body(active_preset="Yolo"), "active_preset"),
    (body(active_preset=["x"]), "active_preset"),
    (body(active_preset={"a": 1}), "active_preset"),
    (body(presets={"Mine": W}), "presets"),
    (body(market_bands={"senior": [200000, 100000]}), "market_bands"),
    (body(bands={"apply_now_min": 50, "worth_a_look_min": 60}), "apply_now_min > worth_a_look_min"),
    (body(bands={"apply_now_min": 60, "worth_a_look_min": 60}), "apply_now_min > worth_a_look_min"),
    (body(bands={"apply_now_min": 70, "worth_a_look_min": 40}), "hard_gate_clamp_ceiling"),
    (body(bands={"apply_now_min": 101, "worth_a_look_min": 50}), "bands"),
    (body(bands={"apply_now_min": 70}), "bands"),
    (body(bands={"apply_now_min": 70.5, "worth_a_look_min": 50}), "bands"),
    ({**body(), "extra": 1}, "extra"),
])
def test_bad_bodies_are_422(client, b, needle):
    r = client.post("/api/jobs/scoring-config", json=b)
    assert r.status_code == 422 and needle in json.dumps(r.json())


def test_missing_compensation_settings_key_is_422(client):
    b = body()
    del b["compensation_settings"]
    r = client.post("/api/jobs/scoring-config", json=b)
    assert r.status_code == 422 and "compensation_settings" in json.dumps(r.json())


def test_a_saved_file_without_compensation_settings_reads_as_defaults(store):
    d = jsc.defaults()
    del d["compensation_settings"]
    store.path.write_text(json.dumps(d), encoding="utf-8")
    doc, warning = store.load()
    assert doc["compensation_settings"]["annual_base_floor"] == 100000 and "unreadable" in warning


def test_a_saved_file_with_non_positive_floor_reads_as_defaults(store):
    d = jsc.defaults()
    d["compensation_settings"]["annual_base_floor"] = 0
    store.path.write_text(json.dumps(d), encoding="utf-8")
    doc, warning = store.load()
    assert doc["compensation_settings"]["annual_base_floor"] == 100000 and warning


def test_bands_save_and_omitting_them_keeps_the_saved_pair(client, store):
    client.post("/api/jobs/scoring-config", json=body(bands={"apply_now_min": 80, "worth_a_look_min": 60}))
    client.post("/api/jobs/scoring-config", json=body(expected_version=1))
    assert json.loads(store.path.read_text(encoding="utf-8"))["bands"] == {"apply_now_min": 80, "worth_a_look_min": 60}


def test_raising_the_ceiling_above_the_saved_bands_is_422(client):
    r = client.post("/api/jobs/scoring-config", json=body(compensation_settings={"annual_base_floor": 100000,
                                                                               "hard_gate_clamp_ceiling": 55}))
    assert r.status_code == 422 and "hard_gate_clamp_ceiling" in json.dumps(r.json())


@pytest.mark.parametrize("bad", [{"apply_now_min": 50, "worth_a_look_min": 60}, {"apply_now_min": 70, "worth_a_look_min": 30}])
def test_a_saved_file_with_invalid_bands_reads_as_defaults(store, bad):
    d = jsc.defaults()
    d["bands"] = bad
    store.path.write_text(json.dumps(d), encoding="utf-8")
    doc, warning = store.load()
    assert doc["bands"] == {"apply_now_min": 75, "worth_a_look_min": 55} and "invalid" in warning


def test_all_zero_weights_are_422(client):
    r = client.post("/api/jobs/scoring-config", json=body(active_weights={k: 0 for k in W}))
    assert r.status_code == 422 and "sum" in json.dumps(r.json())


def test_non_json_body_is_rejected(client):
    r = client.post("/api/jobs/scoring-config", content="active_preset=Balanced",
                    headers={"Content-Type": "application/x-www-form-urlencoded"})
    assert r.status_code in (415, 422)


def test_market_bands_accepted_from_the_derive_script(client, store):
    bands = {**jsc.defaults()["market_bands"], "senior": [118000, 162000]}
    r = client.post("/api/jobs/scoring-config", json=body(market_bands=bands, market_bands_note="from 6 verified postings"))
    assert r.status_code == 200 and r.json()["config"]["market_bands"]["senior"] == [118000, 162000]


def test_corrupt_file_reads_as_defaults_with_warning_and_is_left_alone(client, store):
    store.path.write_text("{not json", encoding="utf-8")
    r = client.get("/api/jobs/scoring-config").json()
    assert r["config"]["version"] == 0 and "unreadable" in r["warning"]
    assert store.path.read_text(encoding="utf-8") == "{not json"


def test_concurrent_saves_never_lose_a_version(store):
    errs = []

    def save(v):
        try:
            store.save(body(expected_version=v))
        except jsc.ConfigError as e:
            errs.append(e.status)
    ts = [threading.Thread(target=save, args=(0,)) for _ in range(5)]
    [t.start() for t in ts]
    [t.join() for t in ts]
    assert json.loads(store.path.read_text(encoding="utf-8"))["version"] == 1 and errs == [409] * 4


def test_home_metros_save_validate_and_survive_a_later_save(client, store):
    r = client.post("/api/jobs/scoring-config", json=body(home_metros=["chicago", "other_us"]))
    assert r.status_code == 200 and r.json()["config"]["home_metros"] == ["chicago", "other_us"]
    client.post("/api/jobs/scoring-config", json=body(expected_version=1))
    assert json.loads(store.path.read_text(encoding="utf-8"))["home_metros"] == ["chicago", "other_us"]
    for bad in ([], ["gotham"], ["remote_us"], ["boston", "boston"], "boston"):
        r = client.post("/api/jobs/scoring-config", json=body(expected_version=2, home_metros=bad))
        assert r.status_code == 422 and "home_metros" in json.dumps(r.json()), bad
