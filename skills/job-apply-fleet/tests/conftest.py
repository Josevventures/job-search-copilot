import sys
from pathlib import Path

import pytest

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
