"""Contract: the Jobs page's skip-reason chips (web/jobs.js) must match jobs.SKIP_REASONS.

jobs.js is plain browser JS with no build step, so the keys are parsed out of its source text.
"""
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import jobs  # noqa: E402

JOBS_JS = Path(__file__).resolve().parents[1] / "web" / "jobs.js"


def _js_skip_reason_keys() -> list[str]:
    src = JOBS_JS.read_text(encoding="utf-8")
    block = re.search(r"const SKIP_REASONS = \[(.*?)\];", src, re.S)
    assert block, "SKIP_REASONS array not found in jobs.js"
    return re.findall(r"key:\s*'([^']+)'", block.group(1))


def test_js_skip_reason_keys_equal_backend():
    assert _js_skip_reason_keys() == list(jobs.SKIP_REASONS)


import json  # noqa: E402
import pytest  # noqa: E402

SCORING_JS = Path(__file__).resolve().parents[1] / "web" / "jobs_scoring.js"


def test_js_factor_keys_equal_backend():
    import jobs_scoring_config
    src = SCORING_JS.read_text(encoding="utf-8")
    block = re.search(r"const FACTORS = \[(.*?)\];", src, re.S)
    assert block, "FACTORS array not found in jobs_scoring.js"
    assert re.findall(r"key:\s*'([^']+)'", block.group(1)) == list(jobs_scoring_config.FACTORS)


def test_js_default_config_equals_backend_defaults():
    import shutil
    import subprocess
    import jobs_scoring_config
    if shutil.which("node") is None:
        pytest.skip("node not on PATH")
    out = subprocess.run(["node", "-e", f"process.stdout.write(JSON.stringify(require({json.dumps(str(SCORING_JS))}).DEFAULT_CONFIG))"],
                         capture_output=True, text=True, encoding="utf-8", check=True).stdout
    js, py = json.loads(out), jobs_scoring_config.defaults()
    for k in ("active_preset", "active_weights", "compensation_settings", "bands", "presets", "market_bands",
              "market_bands_note", "home_metros"):
        assert js[k] == py[k], k
