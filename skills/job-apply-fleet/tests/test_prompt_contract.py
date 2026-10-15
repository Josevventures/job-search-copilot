from conftest import SKILL
from lib.checkpoint import ENUMS, INBOUND_KINDS, METROS, REQUIRED_ALWAYS, REQUIRED_COMPLETE, REQUIRED_V3

PROMPT = (SKILL / "subagent_prompt.md").read_text(encoding="utf-8")
SKILLMD = (SKILL / "SKILL.md").read_text(encoding="utf-8")


def test_prompt_names_every_required_key():
    missing = [k for k in REQUIRED_ALWAYS | REQUIRED_COMPLETE | REQUIRED_V3 if f'"{k}"' not in PROMPT]
    assert not missing, missing


def test_prompt_names_every_enum_value():
    vals = [(k, v) for k, vs in ENUMS.items() for v in vs if v] + [("metro", m) for m in METROS]         + [("inbound", i) for i in INBOUND_KINDS]
    missing = [f"{k}={v}" for k, v in vals if f'"{v}"' not in PROMPT]
    assert not missing, missing


def test_prompt_example_checkpoint_is_valid_v3():
    import json
    import re
    from lib.checkpoint import validate
    block = re.search(r"## 8\. Write the checkpoint.*?```json\n(\{.*?\})\n```", PROMPT, re.S).group(1)
    assert validate(json.loads(block)) == []


def test_prompt_drops_tier_vocabulary():
    for gone in ('"relevance"', '"category"', "moves the lead up one bucket", "Tier 1"):
        assert gone not in PROMPT, gone


def test_prompt_points_at_preferences_and_rubric():
    assert "preferences.md" in PROMPT and "skills/job-apply/SKILL.md" in PROMPT


def test_skill_md_uses_the_scripts_and_never_writes_airtable():
    for s in ("leads_intake.py", "fleet_wave.py", "fleet_merge.py", "--max-agents", "haiku", "sonnet"):
        assert s in SKILLMD, s
    assert "never writes" in SKILLMD.lower()


def test_same_role_early_exit_example_is_a_valid_checkpoint():
    import json
    import re
    from lib.checkpoint import validate
    block = re.search(r"```json\n(\{.*?\})\n```", PROMPT[PROMPT.index('"same_role"'):], re.S).group(1)
    d = json.loads(re.sub(r"<[^>]*>", "x", block))
    assert d["airtable_match"]["relation"] == "same_role"
    assert validate(d) == []


def test_prompt_names_contact_vocabulary():
    from lib.checkpoint import CONTACT_ENUMS
    section = PROMPT[PROMPT.index("## 4. Network check"):PROMPT.index("## 5.")]
    for k, vals in CONTACT_ENUMS.items():
        assert f'"{k}"' in section, k
        for v in vals:
            assert f'"{v}"' in section, v
    for k in ("name", "title", "recommendation"):
        assert f'"{k}"' in section, k
    assert "↑ warm contact" in section



def test_unattended_mode_spells_out_literal_commands():
    """Final-review fix B: headless permission rules match literal commands, so Unattended mode names them."""
    sec = SKILLMD[SKILLMD.index("## Unattended mode"):SKILLMD.index("## Rules")]
    py, sk = "<interpreter>", "skills/job-apply-fleet"
    assert f"`{py} {sk}/fleet_merge.py --batch <batch id>`" in sec
    assert f"`{py} {sk}/fleet_merge.py --summary`" in sec
    assert "`$PY` → the interpreter path from the prompt" in sec and f"`$SK` → `{sk}`" in sec
    assert "`$BATCH` → the batch id" in sec
    assert "each previous batch, in the order listed" in sec


def test_the_wrapper_prompt_and_the_skill_name_the_same_script_dir():
    import jobs_slack_intake as w
    assert w.SK == "skills/job-apply-fleet" and "call Python as " in w.PROMPT


def test_workday_rung_reads_the_prefetched_jd_first_and_curl_only_interactively():
    """Addendum item 1: the nightly wrapper prefetches Workday JDs; the headless run has no curl."""
    line = next(ln for ln in PROMPT.splitlines() if ln.strip().startswith("- Workday:"))
    assert "data/.state/checkpoints/<batch_id>/jd/<lead_id>.txt" in line
    assert "/wday/cxs/<tenant>/<site>/job/" in line and "WebFetch" in line
    assert "curl via Bash only in interactive runs" in line
    assert '"fetch_method": "api"' in line and '"jd_read": true' in line
    assert line.index("jd/<lead_id>.txt") < line.index("WebFetch") < line.index("curl")
