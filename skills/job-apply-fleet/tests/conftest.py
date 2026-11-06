import sys
from pathlib import Path

import pytest

try:  # fail once with the real cause, not one opaque collection error per Slack test module
    from zoneinfo import ZoneInfo
    ZoneInfo("America/New_York")
except Exception as exc:  # pragma: no cover - only under an interpreter without the requirements
    raise pytest.UsageError(
        "job-apply-fleet tests need the tzdata package (Windows has no system time-zone database). This interpreter "
        f"({sys.executable}) does not have it: run `pip install -r requirements-dev.txt` in it, or run the tests "
        "with the interpreter that has the requirements, e.g. `python scripts/run_tests.py` from your venv."
    ) from exc

SKILL = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(SKILL))

FIXTURES = Path(__file__).resolve().parent / "fixtures"


@pytest.fixture
def tmp_dirs(tmp_path, monkeypatch):
    leads = tmp_path / "Leads"
    ckpt = tmp_path / "ckpt"
    summ = tmp_path / "summaries"
    for d in (leads, ckpt, summ):
        d.mkdir()
    monkeypatch.setenv("JOB_LEADS_DIR", str(leads))
    monkeypatch.setenv("JOB_CHECKPOINTS_DIR", str(ckpt))
    monkeypatch.setenv("JOB_SUMMARIES_DIR", str(summ))
    return {"leads": leads, "ckpt": ckpt, "summaries": summ}
