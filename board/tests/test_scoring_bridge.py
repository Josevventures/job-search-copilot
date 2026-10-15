"""Golden fixture through the Python bridge (ranking-engine spec D3). Any Python reader of scores (2b) uses
scoring_bridge and must keep this green. Skipped when node is not on PATH."""
import json
import shutil
import sys
from datetime import datetime
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import scoring_bridge  # noqa: E402

FIXTURE = Path(__file__).resolve().parents[1] / "tests_js" / "fixtures" / "scoring_golden.json"
G = json.loads(FIXTURE.read_text(encoding="utf-8"))
NOW_MS = int(datetime.fromisoformat(G["now"]).timestamp() * 1000)
needs_node = pytest.mark.skipif(shutil.which("node") is None, reason="node not on PATH")


@needs_node
def test_bridge_reproduces_the_synthetic_golden_order_and_raws():
    c = G["synthetic_board"]
    out = scoring_bridge.rank(c["leads"], G["configs"][c["config"]], NOW_MS)
    assert [r["lead_id"] for r in out["ranked"]] == c["expected_order"]
    for r in out["ranked"]:
        assert abs(r["raw"] - c["expected_raw"][r["lead_id"]]) <= 0.1


@needs_node
def test_bridge_reports_needs_retriage():
    out = scoring_bridge.rank([G["edge_cases"][4]["lead"]], G["configs"]["balanced"], NOW_MS)
    assert out["ranked"] == [] and out["needs_retriage"] == ["e5e000000004"]


def test_bridge_without_node_raises(monkeypatch):
    monkeypatch.setattr(scoring_bridge.shutil, "which", lambda name: None)
    with pytest.raises(scoring_bridge.BridgeUnavailable):
        scoring_bridge.rank([], G["configs"]["balanced"], NOW_MS)


def test_bridge_surfaces_a_node_failure():
    class P:
        returncode, stdout, stderr = 1, "", "TypeError: boom"
    with pytest.raises(RuntimeError, match="boom"):
        scoring_bridge.rank([], G["configs"]["balanced"], NOW_MS, run=lambda *a, **k: P(), node="node")


def test_lead_to_view_maps_a_lead_file():
    lead = {"schema_version": 2, "lead_id": "aaaaaaaaaaaa", "intake": {"created_at": "2026-10-01T00:00:00+00:00"},
            "triage": {"company": "Acme"}, "decision": {"decision": None}}
    assert scoring_bridge.lead_to_view(lead) == {"lead_id": "aaaaaaaaaaaa", "schema_version": 2,
                                                 "first_seen": "2026-10-01T00:00:00+00:00",
                                                 "triage": {"company": "Acme"}, "decision": {"decision": None}}


@needs_node
def test_bridge_parity_on_every_edge_case_via_lead_to_view():
    """Python/JS parity: lead-file shape -> lead_to_view -> bridge meets the same expectations as jobs_scoring.test.js
    (incl. Still open freshness, which needs decision passed through)."""
    for c in G["edge_cases"]:
        v, e = c["lead"], c["expect"]
        lead_file = {"lead_id": v["lead_id"], "schema_version": v.get("schema_version", 1),
                     "intake": {"created_at": v.get("first_seen")}, "triage": v["triage"],
                     "decision": v.get("decision") or {}}
        out = scoring_bridge.rank([scoring_bridge.lead_to_view(lead_file)], G["configs"][c["config"]], NOW_MS)
        if e.get("state") == "needs_retriage":
            assert out["needs_retriage"] == [v["lead_id"]], c["name"]
            continue
        [r] = out["ranked"]
        if "gated" in e:
            assert r["gated"] == e["gated"], c["name"]
        if "composite_max" in e:
            assert r["composite"] <= e["composite_max"], c["name"]
        if "freshness" in e:
            assert next(f["score"] for f in r["factors"] if f["key"] == "freshness") == e["freshness"], c["name"]
