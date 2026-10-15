"""python -m prep_extract --url <posting> --out <form_fields.json> [--timeout 120]

Never types, uploads, submits or signs in. Always writes a valid file (a stub on any crash) and exits 0 once the
file is written, so the board can always hand the drafter something to read.

Exit codes: 0 = a file was written (a stub if extraction crashed or timed out); 3 = --out could not be written.
A watchdog bounds the whole run: if extraction has not finished GRACE_S seconds after --timeout, a stub is written
first and then this process tree (Chromium and the Playwright driver included) is killed."""
import argparse
import json
import os
import signal
import subprocess
import sys
import tempfile
import threading
from pathlib import Path

from . import pipeline, schema


def summary(r: dict) -> dict:
    return {"ats": r.get("ats"), "adapter": r.get("adapter"), "questions_visible": r.get("questions_visible"),
            "sign_in_required": r.get("sign_in_required"), "pages": len(r.get("pages") or []),
            "fields": schema.field_count(r),
            "clicks": sum(1 for c in r.get("click_log") or [] if c.get("decision") == "clicked"),
            "unseen_pages": r.get("unseen_pages"), "errors": len(r.get("errors") or [])}


def write_json_atomic(path, data) -> None:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=str(path.parent), prefix=path.name + ".", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


GRACE_S = 15  # seconds past --timeout before the watchdog gives up on the extractor


def kill_process_tree() -> None:
    """End this process and every child (Chromium, the Playwright driver). Only called after the stub is written."""
    pid = os.getpid()
    if os.name == "nt":
        subprocess.run(["taskkill", "/T", "/F", "/PID", str(pid)], capture_output=True)
    else:
        subprocess.run(["pkill", "-KILL", "-P", str(pid)], capture_output=True)
        os.kill(pid, signal.SIGKILL)
    os._exit(0)  # only reached if the kill did not end us


def run(argv=None, kill=None, join_s=None) -> int:
    kill = kill or kill_process_tree
    ap = argparse.ArgumentParser(prog="prep_extract", description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--url", required=True)
    ap.add_argument("--out", required=True)
    ap.add_argument("--timeout", type=int, default=120)
    a = ap.parse_args(argv)
    box: dict = {}

    def work():
        try:
            box["result"] = pipeline.extract(a.url, timeout_s=a.timeout)
        except Exception as e:  # never fail the job: a crash still yields a valid, empty form
            box["result"] = schema.stub_result(a.url, f"extractor crashed: {type(e).__name__}: {e}")

    worker = threading.Thread(target=work, daemon=True)
    worker.start()
    worker.join(join_s if join_s is not None else max(10, a.timeout) + GRACE_S)
    timed_out = worker.is_alive()
    result = schema.stub_result(a.url, f"extraction timed out after {a.timeout} s") if timed_out else box["result"]
    try:
        write_json_atomic(a.out, result)
    except OSError as e:
        sys.stderr.write(f"prep_extract: cannot write {a.out}: {e}\n")
        if timed_out:
            kill()
        return 3
    sys.stdout.write(json.dumps(summary(result)) + "\n")
    sys.stdout.flush()
    if timed_out:
        kill()
    return 0


def main(argv=None) -> int:
    return run(argv)
