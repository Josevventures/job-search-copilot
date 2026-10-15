"""Recompute config market_bands from verified postings (ranking-engine spec §6.2; plan Task 19, GATE 5).
Prints the diff; saves through POST /api/jobs/scoring-config only with --apply, after you have read the diff."""
import argparse
import json
import os
import statistics
import sys
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from lib.lead_store import LeadStore  # noqa: E402

PERIOD = {"year": 1, "hour": 2080, "month": 12}
MIN_N = 3


def derive(leads, current, home=()):
    seen = {}
    for l in leads:
        t = l.get("triage") or {}
        if l.get("schema_version", 1) < 2 or t.get("pay_source") not in ("posting", "ats_api"):
            continue
        bands = [b for b in t.get("pay_bands") or [] if b.get("basis") == "base" and b.get("period") in PERIOD]
        if not bands or t.get("level") not in current:
            continue
        b = next((x for x in bands if x.get("location_metro") in home), bands[0])
        seen.setdefault(t["level"], []).append((b["min"] * PERIOD[b["period"]], b["max"] * PERIOD[b["period"]]))
    out, counts = dict(current), {k: len(v) for k, v in seen.items()}
    for level, pairs in seen.items():
        if len(pairs) >= MIN_N:
            out[level] = [int(statistics.median(p[0] for p in pairs)), int(statistics.median(p[1] for p in pairs))]
    return out, counts


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("--base", default=f"http://127.0.0.1:{os.environ.get('PORT', '8420')}")
    ap.add_argument("--apply", action="store_true")
    a = ap.parse_args(argv)
    with urllib.request.urlopen(a.base + "/api/jobs/scoring-config", timeout=15) as r:
        cfg = json.loads(r.read())["config"]
    bands, counts = derive(LeadStore().list(), cfg["market_bands"], tuple(cfg.get("home_metros") or ()))
    for k in cfg["market_bands"]:
        print(f"{k:14} {cfg['market_bands'][k]} -> {bands[k]}  (verified: {counts.get(k, 0)})")
    if not a.apply:
        print("dry run: nothing saved")
        return 0
    n = sum(c for k, c in counts.items() if c >= MIN_N)
    body = {"expected_version": cfg["version"], "active_preset": cfg["active_preset"],
            "active_weights": cfg["active_weights"], "compensation_settings": cfg["compensation_settings"],
            "market_bands": bands, "market_bands_note": f"from {n} verified postings"}
    req = urllib.request.Request(a.base + "/api/jobs/scoring-config", data=json.dumps(body).encode("utf-8"),
                                 headers={"Content-Type": "application/json"}, method="POST")
    with urllib.request.urlopen(req, timeout=15) as r:
        print("saved version", json.loads(r.read())["config"]["version"])
    return 0


if __name__ == "__main__":
    sys.exit(main())
