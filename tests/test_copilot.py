"""The glue: config loading and module switches, the demo seeding, and the sample leads the demo board shows."""
import json
import shutil
import sys
from datetime import date
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "board"))

from copilot import cli, config  # noqa: E402

SAMPLES = sorted((ROOT / "samples" / "leads").glob("*.json"))


def test_example_config_has_every_module_off():
    cfg = config.load()
    assert set(cfg["modules"]) == set(config.MODULES)
    assert not any(config.enabled(m, cfg) for m in config.MODULES)


def test_a_partial_config_is_merged_over_the_example(tmp_path, monkeypatch):
    own = tmp_path / "config.yaml"
    own.write_text("data_dir: mydata\nmodules:\n  airtable:\n    enabled: true\n", encoding="utf-8")
    monkeypatch.setenv("JSC_CONFIG", str(own))
    monkeypatch.delenv("JSC_DATA_DIR", raising=False)
    cfg = config.load()
    assert config.enabled("airtable", cfg) and not config.enabled("slack_intake", cfg)
    assert cfg["modules"]["airtable"]["fields"]["role_url"] == "Role URL"     # kept from the example
    assert config.data_dir(cfg) == ROOT / "mydata" and config.leads_dir(cfg) == ROOT / "mydata" / "leads"


def test_data_dir_env_override(monkeypatch, tmp_path):
    monkeypatch.setenv("JSC_DATA_DIR", str(tmp_path))
    assert config.data_dir() == tmp_path and config.prep_dir() == tmp_path / "prep"


def test_unknown_module_is_an_error():
    with pytest.raises(KeyError):
        config.module("email")


def test_samples_are_invented_leads_in_the_live_schema():
    assert 15 <= len(SAMPLES) <= 25
    routes = {}
    for f in SAMPLES:
        lead = json.loads(f.read_text(encoding="utf-8"))
        assert f.stem == lead["lead_id"]
        t = lead["triage"]
        routes[t.get("route") or t.get("triage_status")] = routes.get(t.get("route") or t.get("triage_status"), 0) + 1
    assert routes.get("board", 0) >= 12 and routes.get("closed") == 1 and routes.get("pending") == 1


def test_demo_seed_shifts_every_date_to_today(tmp_path):
    n = cli._seed_demo(tmp_path / "demo")
    assert n == len(SAMPLES)
    days = (date.today() - cli.ANCHOR).days
    for f in (tmp_path / "demo" / "leads").glob("*.json"):
        lead = json.loads(f.read_text(encoding="utf-8"))
        src = json.loads((ROOT / "samples" / "leads" / f.name).read_text(encoding="utf-8"))
        pd, spd = lead["triage"].get("posted_date"), src["triage"].get("posted_date")
        if spd:
            assert (date.fromisoformat(pd) - date.fromisoformat(spd)).days == days
    cli._seed_demo(tmp_path / "demo")                                 # re-seeding starts clean
    assert len(list((tmp_path / "demo" / "leads").glob("*.json"))) == n


@pytest.mark.skipif(shutil.which("node") is None, reason="node not on PATH")
def test_the_demo_board_ranks_through_the_one_scorer(tmp_path):
    import jobs_scoring_config
    import scoring_bridge
    cli._seed_demo(tmp_path / "demo")
    leads = [json.loads(f.read_text(encoding="utf-8")) for f in (tmp_path / "demo" / "leads").glob("*.json")]
    now_ms = int(__import__("time").time() * 1000)
    out = scoring_bridge.rank([scoring_bridge.lead_to_view(l) for l in leads], jobs_scoring_config.defaults(), now_ms)
    assert len(out["ranked"]) >= 14                     # the closed card ranks too; Closed? is a board section
    assert len(out["needs_retriage"]) == len(leads) - len(out["ranked"])
    scores = [r["composite"] for r in out["ranked"]]
    assert scores == sorted(scores, reverse=True) and all(1 <= s <= 100 for s in scores)
    assert any(r["gated"] for r in out["ranked"])       # the equity-only sample is capped by the pay floor


def test_cli_modules_prints_the_switches(capsys):
    assert cli.main(["modules"]) == 0
    out = capsys.readouterr().out
    assert '"airtable": false' in out and "data dir:" in out
