"""python -m copilot <command>

  demo                 copy the invented sample leads into a scratch data dir and open the ranked board
  serve                run the board on your own data dir (config.yaml data_dir)
  add URL... | --file F | --xlsx F
                       turn job links into lead files (link/file intake; no AI, no API key)
  modules              show which optional modules are on

Everything here is a thin wrapper: `serve` runs board/server.py, `add` runs skills/job-apply-fleet/leads_intake.py.
"""
import argparse
import json
import os
import shutil
import sys
import threading
import webbrowser
from datetime import date, datetime, timedelta
from pathlib import Path

from copilot import config

SAMPLES = config.ROOT / "samples" / "leads"
DEMO_DIR = config.ROOT / "data-demo"


def _serve(host: str, port: int, open_browser: bool) -> int:
    import uvicorn
    url = f"http://{host}:{port}/jobs"
    print(f"Jobs board: {url}  (Ctrl+C to stop)")
    if open_browser:
        threading.Timer(1.5, lambda: webbrowser.open(url)).start()
    uvicorn.run("board.server:app", host=host, port=port, log_level="warning")
    return 0


ANCHOR = date(2026, 10, 15)   # the date samples/make_samples.py wrote the sample leads against


def _shift(value, days: int):
    """Move an ISO date or datetime string by `days`; anything else comes back unchanged."""
    if not isinstance(value, str) or not days:
        return value
    try:
        if len(value) == 10:
            return (date.fromisoformat(value) + timedelta(days=days)).isoformat()
        return (datetime.fromisoformat(value) + timedelta(days=days)).isoformat()
    except ValueError:
        return value


def _age_to_today(lead: dict, days: int) -> dict:
    """Shift every date a lead carries, so posting ages on the demo board look the same on any day."""
    intake, triage = lead.get("intake") or {}, lead.get("triage") or {}
    intake["created_at"] = _shift(intake.get("created_at"), days)
    for s in intake.get("sources") or []:
        s["added_at"] = _shift(s.get("added_at"), days)
    for k in ("posted_date", "triaged_at"):
        if k in triage:
            triage[k] = _shift(triage[k], days)
    return lead


def _seed_demo(target: Path) -> int:
    """A fresh copy every time, so the demo always starts from the same board. Never touches your data dir."""
    if target.exists():
        shutil.rmtree(target)
    leads = target / "leads"
    leads.mkdir(parents=True)
    n = 0
    days = (date.today() - ANCHOR).days
    for f in sorted(SAMPLES.glob("*.json")):
        lead = _age_to_today(json.loads(f.read_text(encoding="utf-8")), days)
        (leads / f.name).write_text(json.dumps(lead, ensure_ascii=False, indent=2), encoding="utf-8")
        n += 1
    return n


def cmd_demo(a) -> int:
    n = _seed_demo(DEMO_DIR)
    os.environ["JSC_DATA_DIR"] = str(DEMO_DIR)
    os.environ["JSC_DEMO"] = "1"   # board/server.py: Applied and Prep are simulated, see board/demo.py
    print(f"Seeded {n} invented leads into {DEMO_DIR.name}/ (recreated on every `demo`).")
    cfg = config.load()
    return _serve(a.host or cfg["board"]["host"], a.port or cfg["board"]["port"], not a.no_browser)


def cmd_serve(a) -> int:
    cfg = config.load()
    config.leads_dir(cfg).mkdir(parents=True, exist_ok=True)
    return _serve(a.host or cfg["board"]["host"], a.port or cfg["board"]["port"], not a.no_browser)


def cmd_add(a, rest) -> int:
    skill = config.ROOT / "skills" / "job-apply-fleet"
    sys.path.insert(0, str(skill))
    import leads_intake
    return leads_intake.main(rest)


def cmd_modules(a) -> int:
    cfg = config.load()
    print(f"config: {config.config_path()}")
    print(f"data dir: {config.data_dir(cfg)}")
    print(json.dumps({m: config.enabled(m, cfg) for m in config.MODULES}, indent=2))
    return 0


def main(argv=None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    if argv and argv[0] == "add":   # everything after `add` goes to leads_intake.py unchanged
        return cmd_add(None, argv[1:])
    ap = argparse.ArgumentParser(prog="python -m copilot", description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    for name in ("demo", "serve"):
        p = sub.add_parser(name)
        p.add_argument("--host")
        p.add_argument("--port", type=int)
        p.add_argument("--no-browser", action="store_true")
    sub.add_parser("add", help="see `python -m copilot add --help`")
    sub.add_parser("modules")
    a = ap.parse_args(argv)
    return {"demo": cmd_demo, "serve": cmd_serve, "modules": cmd_modules}[a.cmd](a)
