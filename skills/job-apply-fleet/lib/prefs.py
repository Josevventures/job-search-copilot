import json
import re
from pathlib import Path

from lib import paths


def load(path: Path | None = None) -> dict:
    text = Path(path or paths.preferences_file()).read_text(encoding="utf-8")
    m = re.search(r"```json\s*(\{.*?\})\s*```", text, re.S)
    if not m:
        raise ValueError("the preferences file has no ```json block")
    return json.loads(m.group(1))


def screen_labels(prefs: dict) -> list[str]:
    """Top-level key since 2026-10-13; the old auto_skip.screen_labels shape is still read."""
    return list(prefs.get("screen_labels") or (prefs.get("auto_skip") or {}).get("screen_labels") or [])
