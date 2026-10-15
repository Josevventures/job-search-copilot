"""Default locations for lead data and checkpoints. Env vars override for tests.

Everything lives under the data dir from config.yaml (copilot/config.py). The repo root is found by walking up
from this file, so the skill works from skills/job-apply-fleet/ and from a copy in .claude/skills/."""
import os
import sys
from pathlib import Path

SKILL_DIR = Path(__file__).resolve().parent.parent


def _find_root(start: Path) -> Path:
    env = os.environ.get("JSC_HOME")
    if env:
        return Path(env)
    for p in [start, *start.parents]:
        if (p / "copilot" / "config.py").is_file():
            return p
    raise RuntimeError("cannot find the job-search-copilot repo root (copilot/config.py); set JSC_HOME")


REPO_ROOT = _find_root(SKILL_DIR)
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from copilot import config  # noqa: E402


def leads_dir() -> Path:
    return Path(os.environ.get("JOB_LEADS_DIR") or config.leads_dir())


def checkpoints_dir() -> Path:
    return Path(os.environ.get("JOB_CHECKPOINTS_DIR") or config.state_dir() / "checkpoints")


def summaries_dir() -> Path:
    return Path(os.environ.get("JOB_SUMMARIES_DIR") or config.summaries_dir())


def slack_intake_dir() -> Path:
    return Path(os.environ.get("JOB_SLACK_INTAKE_DIR") or config.state_dir() / "slack-intake")


def preferences_file() -> Path:
    """The triage preferences the subagents read. Falls back to the shipped example."""
    rel = config.module("ai_triage").get("preferences_file")
    p = config.resolve(rel) if rel else SKILL_DIR / "preferences.md"
    return p if p.is_file() else SKILL_DIR / "preferences.example.md"
