"""Stage 3 helper for /job-apply-fleet: manifest, preflight estimate, wave mark/collect. No LLM."""
import argparse
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from lib import paths  # noqa: E402
from lib.checkpoint import validate  # noqa: E402
from lib.lead_store import LeadStore  # noqa: E402
from lib.prefs import load as load_prefs, screen_labels  # noqa: E402

CAP = 6
TOKENS_PER_AGENT = 70_000


def _mpath(batch_id):
    return paths.checkpoints_dir() / batch_id / "_manifest.json"


def _load(batch_id):
    return json.loads(_mpath(batch_id).read_text(encoding="utf-8"))


def _save(batch_id, m):
    _mpath(batch_id).write_text(json.dumps(m, indent=2, ensure_ascii=False), encoding="utf-8")


def _waves(m):
    ids = list(m["leads"])
    return [ids[i:i + m["concurrency_cap"]] for i in range(0, len(ids), m["concurrency_cap"])]


def select_leads(store, retriage, only_failed, prefs, ids=None):
    idset = set(ids) if ids is not None else None
    out = []
    for lead in store.list():
        if idset is not None and lead["lead_id"] not in idset:
            continue
        t, i = lead.get("triage", {}), lead.get("intake", {})
        if (t.get("airtable_match") or {}).get("relation") == "same_role":
            continue
        if (i.get("screen") or {}).get("label") in screen_labels(prefs):
            continue
        status = t.get("triage_status", "pending")
        if only_failed:
            if status == "failed":
                out.append(lead)
        elif retriage or status in ("pending", "failed"):
            out.append(lead)
    return out


def init(batch_id, store, retriage, only_failed, max_agents, yes, prefs, ids=None, limit=None):
    if _mpath(batch_id).exists():
        print(f"REFUSED: batch {batch_id} already has a manifest. Run fleet_merge.py --batch {batch_id} to merge "
              f"its checkpoints, then init a NEW batch id.")
        return 2
    leads = select_leads(store, retriage, only_failed, prefs, ids)
    carried = []
    if limit is not None:  # unattended (Slack) runs: take the first N in --ids-file order, carry the rest
        if ids is not None:
            pos = {lid: i for i, lid in enumerate(ids)}
            leads.sort(key=lambda l: pos.get(l["lead_id"], len(pos)))
        carried = [l["lead_id"] for l in leads[limit:]]
        leads = leads[:limit]
    k = len(leads)
    waves = -(-k // CAP) if k else 0
    print(f"{k} leads -> {k} agents in {waves} waves at cap {CAP}, ~{k * TOKENS_PER_AGENT / 1e6:.1f}M tokens"
          + (f"; {len(carried)} carried to the next run" if carried else ""))
    if limit is None and k > max_agents and not yes:
        print(f"REFUSED: {k} > --max-agents {max_agents}. Confirm the cost with the user, then re-run with --yes.")
        return 3
    _mpath(batch_id).parent.mkdir(parents=True, exist_ok=True)
    _save(batch_id, {"batch_id": batch_id, "created_at": datetime.now(timezone.utc).isoformat(),
                     "concurrency_cap": CAP, "carried": carried,
                     "leads": {l["lead_id"]: {"status": "pending", "url": l["canonical_url"]} for l in leads}})
    return 0


def dispatch_lines(batch_id, n, store):
    lines = []
    for lid in _waves(_load(batch_id))[n - 1]:
        lead = store.read(lid)
        i = lead.get("intake", {})
        src = (i.get("sources") or [{}])[0]
        hint = src.get("hint") or (i.get("preview") or {}).get("title") or "none"
        jd = ",".join(i.get("jd_links") or []) or "none"
        note = next((s.get("note") for s in reversed(i.get("sources") or []) if s.get("note")), None) or "none"
        lines.append(f"{lid} | {lead['canonical_url']} | jd_links={jd} | hint={hint} | note={note}")
    return lines


def mark(batch_id, n):
    m = _load(batch_id)
    for lid in _waves(m)[n - 1]:
        m["leads"][lid]["status"] = "in_progress"
    _save(batch_id, m)


def collect(batch_id, n):
    m = _load(batch_id)
    for lid in _waves(m)[n - 1]:
        p = _mpath(batch_id).parent / f"{lid}.json"
        status = "failed"
        if p.exists():
            try:
                d = json.loads(p.read_text(encoding="utf-8"))
                if not validate(d):
                    status = d["status"]
            except json.JSONDecodeError:
                pass
        m["leads"][lid]["status"] = status
    _save(batch_id, m)
    vals = [v["status"] for v in m["leads"].values()]
    return {"complete": vals.count("complete"), "failed": vals.count("failed"), "total": len(vals)}


def over_half_failed(batch_id):
    vals = [v["status"] for v in _load(batch_id)["leads"].values()]
    return vals.count("failed") * 2 > len(vals)


def write_result(batch_id, out_path, airtable_dedupe="ok", stopped=False):
    """result.json for unattended runs (jobs-slack-intake plan D3): derived from the manifest, never model-written."""
    m = _load(batch_id)
    by = lambda s: [lid for lid, v in m["leads"].items() if v["status"] == s]  # noqa: E731
    res = {"batch_id": batch_id, "triaged": by("complete"), "failed": by("failed"),
           "carried": list(m.get("carried") or []), "airtable_dedupe": airtable_dedupe, "stopped": bool(stopped)}
    Path(out_path).write_text(json.dumps(res, indent=2), encoding="utf-8")
    return res


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("cmd", choices=["init", "show", "mark", "collect", "status", "result"])
    ap.add_argument("n", nargs="?", type=int)
    ap.add_argument("--batch", required=True)
    ap.add_argument("--retriage", action="store_true")
    ap.add_argument("--only-failed", action="store_true")
    ap.add_argument("--max-agents", type=int, default=20)
    ap.add_argument("--yes", action="store_true")
    ap.add_argument("--ids-file")
    ap.add_argument("--limit", type=int)
    ap.add_argument("--out")
    ap.add_argument("--airtable-dedupe", choices=["ok", "skipped"], default="ok")
    ap.add_argument("--stopped", action="store_true")
    a = ap.parse_args(argv)
    if a.cmd == "result" and not a.out:
        ap.error("result needs --out <result path>")
    store = LeadStore()
    if a.cmd == "init":
        ids = None
        if a.ids_file:
            ids = []
            for ln in Path(a.ids_file).read_text(encoding="utf-8").splitlines():
                ln = ln.strip()
                if ln and not ln.startswith("#") and ln not in ids:
                    ids.append(ln)
        return init(a.batch, store, a.retriage, a.only_failed, a.max_agents, a.yes, load_prefs(), ids, a.limit)
    if a.cmd == "result":
        print(json.dumps(write_result(a.batch, a.out, a.airtable_dedupe, a.stopped)))
        return 0
    if a.cmd == "show":
        print("\n".join(dispatch_lines(a.batch, a.n, store)))
    elif a.cmd == "mark":
        mark(a.batch, a.n)
        print(f"wave {a.n} in_progress")
    elif a.cmd == "collect":
        rep = collect(a.batch, a.n)
        print(json.dumps(rep))
        if over_half_failed(a.batch):
            print("STOP: more than half of this batch failed")
    else:
        m = _load(a.batch)
        print(f"{len(_waves(m))} waves; " + json.dumps(
            {s: [v["status"] for v in m["leads"].values()].count(s) for s in ("pending", "in_progress", "complete", "failed")}))
    return 0


if __name__ == "__main__":
    sys.exit(main())
