"""The Jobs board server: one FastAPI app that serves the page, the lead API and the ranking-config API.

    python -m copilot serve            (or: uvicorn board.server:app --port 8420)

The private system mounts these same routers inside a larger personal dashboard; this file is the thin standalone
host for them. The sweeper thread (90 s pending actions, dispatched-job polling) starts with the app lifespan, not
at import, so tests that import this module never touch real lead files.

No authentication: bind to 127.0.0.1 (the default), or to a private network you trust.
"""
import os
import sys
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse, RedirectResponse, Response

BOARD = Path(__file__).resolve().parent
if str(BOARD) not in sys.path:
    sys.path.insert(0, str(BOARD))

import jobs as jobs_mod  # noqa: E402  (puts the repo root and the skill dir on sys.path)
import jobs_scoring_config  # noqa: E402
from copilot import config  # noqa: E402

WEB = BOARD / "web"
JOBS_PAGE, JOBS_JS, JOBS_SCORING_JS = WEB / "jobs.html", WEB / "jobs.js", WEB / "jobs_scoring.js"


def build(leads_dir: Path | None = None, config_path: Path | None = None, dispatch_enabled: bool | None = None,
          demo: bool | None = None):
    """(JobsService, ConfigStore) for this process. Arguments override config.yaml (tests, a demo data dir).
    demo (default: JSC_DEMO=1, set by `python -m copilot demo`) simulates Applied and Prep; see board/demo.py."""
    store = jobs_scoring_config.ConfigStore(Path(config_path) if config_path else config.scoring_config_path())
    if dispatch_enabled is None:
        dispatch_enabled = config.enabled("job_apply")
    if demo is None:
        demo = os.environ.get("JSC_DEMO") == "1"
    svc = jobs_mod.JobsService(jobs_mod.REPO_ROOT, leads_dir=leads_dir, config_store=store,
                               dispatch_enabled=dispatch_enabled, demo=demo)
    return svc, store


JOBS, JOBS_CONFIG = build()


@asynccontextmanager
async def _lifespan(app):
    stop = jobs_mod.start_sweeper(JOBS)
    try:
        yield
    finally:
        stop.set()


app = FastAPI(title="job-search-copilot board", lifespan=_lifespan)
app.include_router(jobs_mod.make_router(JOBS))
app.include_router(jobs_scoring_config.make_router(JOBS_CONFIG))

_NO_STORE = {"Cache-Control": "no-store"}


@app.get("/")
def index():
    return RedirectResponse("/jobs")


@app.get("/jobs")
def jobs_page():
    return FileResponse(JOBS_PAGE, media_type="text/html; charset=utf-8", headers=_NO_STORE)


@app.get("/jobs.js")
def jobs_script():
    return FileResponse(JOBS_JS, media_type="application/javascript; charset=utf-8", headers=_NO_STORE)


@app.get("/jobs_scoring.js")
def jobs_scoring_script():
    return FileResponse(JOBS_SCORING_JS, media_type="application/javascript; charset=utf-8", headers=_NO_STORE)


@app.get("/api/files/{path:path}")
def prep_file(path: str):
    """A finished prep pack, for the card's "Prep ready" link. Only Markdown files inside the prep folder."""
    base = config.prep_dir().resolve()
    try:
        repo_rel = (jobs_mod.REPO_ROOT / path).resolve()
    except (OSError, ValueError):
        raise HTTPException(status_code=404, detail="not found")
    if not (repo_rel.is_relative_to(base) and repo_rel.suffix == ".md" and repo_rel.is_file()):
        raise HTTPException(status_code=404, detail="not found")
    # text/plain so every browser shows the pack instead of downloading it
    return Response(repo_rel.read_bytes(), media_type="text/plain; charset=utf-8", headers=_NO_STORE)


@app.get("/api/health")
def health():
    return {"ok": True, "demo": JOBS.demo, "modules": {m: config.enabled(m) for m in config.MODULES},
            "leads_dir": str(JOBS.store.base), "pid": os.getpid()}
