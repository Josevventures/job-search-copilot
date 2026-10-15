"""Test-session defaults: the example config is the config (data_dir "data", every module off) and no test
touches a real lead: each test builds its service on a tmp folder. Dispatch's job list goes to a temp file."""
import os

import pytest

os.environ.pop("JSC_CONFIG", None)
os.environ.pop("JSC_DATA_DIR", None)


@pytest.fixture(autouse=True)
def _isolated_job_list(tmp_path, monkeypatch):
    import dispatch
    monkeypatch.setattr(dispatch, "JOBS_FILE", tmp_path / "jobs-autouse.json")
    monkeypatch.setattr(dispatch, "LOG_FILE", tmp_path / "dispatch-log.md")
