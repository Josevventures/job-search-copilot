"""Apply the user's confirmed closed list through the running dashboard's own Skip action:
90 s undo, decision snapshot, _skips.jsonl line with reason closed_stale, then the sweeper deletes the file.
Refuses any id that is not both confirmed by the user and marked closed in the report."""
import argparse
import json
import os
import sys
import urllib.error
import urllib.request
from pathlib import Path


def apply(confirmed_ids, report, post, date) -> dict:
    closed = {r["lead_id"] for r in report["results"] if r["verdict"] == "closed"}
    res = {"posted": [], "refused": [], "failed": []}
    for lid in confirmed_ids:
        if lid not in closed:
            res["refused"].append(lid)
            continue
        status, _ = post(f"/api/jobs/leads/{lid}/action", {"action": "skip", "reason": "closed_stale",
                                                            "note": f"closed-check {date} (user confirmed)"})
        res["posted" if status == 200 else "failed"].append(lid)
    return res


def _poster(base):
    def post(path, body):
        req = urllib.request.Request(base + path, data=json.dumps(body).encode("utf-8"),
                                     headers={"Content-Type": "application/json"}, method="POST")
        try:
            with urllib.request.urlopen(req, timeout=15) as r:
                return r.status, json.loads(r.read() or b"{}")
        except urllib.error.HTTPError as e:
            return e.code, {}
    return post


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("--report", required=True)
    ap.add_argument("--confirmed", required=True)
    ap.add_argument("--base", default=f"http://127.0.0.1:{os.environ.get('PORT', '8420')}")
    a = ap.parse_args(argv)
    report = json.loads(Path(a.report).read_text(encoding="utf-8"))
    ids = [l.strip() for l in Path(a.confirmed).read_text(encoding="utf-8").splitlines() if l.strip() and not l.startswith("#")]
    print(json.dumps(apply(ids, report, _poster(a.base), report["date"]), indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
