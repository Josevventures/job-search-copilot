"""The only sanctioned Python path to a Jobs score (ranking-engine spec D1/D3).

Python never re-implements a factor or the composite: it pipes leads to web/jobs_scoring.js (Node CLI).
Used by the golden-fixture test and the Slack intake replies. Stdlib only."""
import json
import shutil
import subprocess
from pathlib import Path

SCORER = Path(__file__).resolve().parent / "web" / "jobs_scoring.js"


class BridgeUnavailable(RuntimeError):
    pass


def lead_to_view(lead: dict) -> dict:
    """A lead file -> the view shape the scorer reads (what GET /api/jobs/leads returns, scoring keys only)."""
    return {"lead_id": lead.get("lead_id"), "schema_version": lead.get("schema_version", 1),
            "first_seen": (lead.get("intake") or {}).get("created_at"), "triage": lead.get("triage") or {},
            "decision": lead.get("decision") or {}}   # the scorer reads decision.still_open (freshness)


def rank(leads, config, now_ms, run=subprocess.run, timeout_s=20, node=None) -> dict:
    node = node or shutil.which("node")
    if not node:
        raise BridgeUnavailable("node is not on PATH; the Jobs scorer runs only in JS")
    p = run([node, str(SCORER)], input=json.dumps({"leads": leads, "config": config, "now_ms": now_ms}),
            capture_output=True, text=True, encoding="utf-8", timeout=timeout_s)
    if p.returncode != 0:
        raise RuntimeError(f"jobs_scoring.js exit {p.returncode}: {(p.stderr or '').strip()[-300:]}")
    return json.loads(p.stdout)
