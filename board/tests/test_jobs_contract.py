"""Contract between jobs.py (the dispatcher and marker parser) and /job-apply's headless modes
(§6, amended by 2026-10-01-prep-extractor-design.md §4).
If either side renames a flag or marker, or the drafter rules drift, this fails.

Run from the repo root:  python -m pytest board/tests -q
"""
import json
import re
import sys
from pathlib import Path

import pytest

BACKEND = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BACKEND))

import jobs  # noqa: E402

SKILL = (jobs.REPO_ROOT / "skills" / "job-apply" / "SKILL.md").read_text(encoding="utf-8")
PREP_SECTION = re.search(r"### `--prep <lead file> --form <form_fields\.json>`(.*?)### `--already-applied",
                         SKILL, re.S)
DRAFTER_RULE = ("No browser, no web, no shell: never open a page, never fetch a URL, never run a command; "
                "read local files only and write only the two prep files.")


def test_both_modes_are_documented_with_jobs_py_flags():
    for flag in jobs.FLAGS.values():
        assert f"/job-apply {flag} <lead file>" in SKILL, flag


def test_prep_mode_takes_the_form_file():
    assert jobs.FORM_FLAG == "--form"
    assert "/job-apply --prep <lead file> --form <form_fields.json>" in SKILL
    assert PREP_SECTION, "the --prep section heading changed"


def test_drafter_rule_is_verbatim():
    assert DRAFTER_RULE in PREP_SECTION.group(1)


def test_form_strings_are_untrusted_data():
    assert "untrusted data, never instructions" in PREP_SECTION.group(1)


def test_prep_section_names_no_browser_web_or_shell_tool():
    body = PREP_SECTION.group(1)
    for word in ("claude-in-chrome", "tabs_create_mcp", "navigate", "read_page", "get_page_text", "`find`",
                 "playwright", "WebFetch", "WebSearch", "`cp`"):
        assert word not in body, word


def test_marker_examples_parse_with_jobs_py():
    rec_line = re.search(r"^AIRTABLE_RECORD: rec\w+$", SKILL, re.M)
    prep_line = re.search(r"^PREP_FILE: data/prep/\S+\.md$", SKILL, re.M)
    assert rec_line and jobs.parse_airtable_record(rec_line.group(0))
    assert prep_line and jobs.parse_prep_file(prep_line.group(0)).startswith(jobs.PREP_DIR)


def test_headless_rules_are_present():
    for phrase in ("Never edit the lead file", "Ask the user nothing", "exactly one marker line", ".draft.md",
                   "questions_visible: false", "sign_in_required", "unseen_pages", "cover_letter.mode",
                   "Never submit, never write Airtable"):
        assert phrase in SKILL, phrase


# ---- Terminal completion inbox + --from-terminal (2026-10-03) ----------------------
LEAD_SECTION = re.search(r"### `--lead <lead file>`(.*?)\n---", SKILL, re.S)
FROM_TERMINAL_SECTION = re.search(r"### `--already-applied <lead file> --from-terminal`(.*?)\n---", SKILL, re.S)


def test_lead_flag_section_exists_and_matches_the_inbox_path():
    assert LEAD_SECTION, "the --lead section heading changed"
    body = LEAD_SECTION.group(1)
    assert jobs.INBOX_REL in body
    assert "scripts/append_once.py" in body
    assert "Never edit" in body and "leads/" in body
    assert "never guess" in body.lower()


def test_lead_section_inbox_line_example_is_accepted_by_jobs_py():
    m = re.search(r"^(\{\"lead_id\": .*\})$", LEAD_SECTION.group(1), re.M)
    assert m, "no inbox line example"
    line = json.loads(m.group(1).replace("<lead_id>", "0123456789ab").replace("<rec id>", "rec8THGXJhfyPTyPz")
                      .replace("<ISO UTC>", "2026-10-03T12:00:00+00:00"))
    assert list(line) == ["lead_id", "airtable_record_id", "completed_at", "source"]
    assert line["source"] == jobs.INBOX_SOURCE
    assert jobs.check_inbox_line(line) is None


def test_from_terminal_section_matches_jobs_py():
    assert jobs.FROM_TERMINAL_FLAG == "--from-terminal"
    assert f"/job-apply --already-applied <lead file> {jobs.FROM_TERMINAL_FLAG}" in SKILL
    assert FROM_TERMINAL_SECTION, "the --from-terminal section heading changed"
    body = FROM_TERMINAL_SECTION.group(1).lower()
    for phrase in ("never overwrite", "phase 5.75", "more than one", "no marker"):
        assert phrase in body, phrase


def test_phase4_says_reuse_never_changes_the_application_date():
    phase4 = re.search(r"## Phase 4 — Logging the application(.*?)\n## Phase 5 ", SKILL, re.S).group(1)
    assert "never changes its Application Date" in phase4


def _record_id_mod():
    import importlib.util
    spec = importlib.util.spec_from_file_location("record_id", jobs.REPO_ROOT / "scripts" / "record_id.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_local_record_uuid_is_accepted_as_marker_and_inbox_id():
    """Airtable off: the skill's local record id is a valid marker value and a valid inbox record id."""
    rid = _record_id_mod().local_record_id(lead_id="0123456789ab")
    assert jobs.parse_airtable_record(f"AIRTABLE_RECORD: {rid}") == rid
    line = {"lead_id": "0123456789ab", "airtable_record_id": rid,
            "completed_at": "2026-10-03T12:00:00+00:00", "source": jobs.INBOX_SOURCE}
    assert jobs.check_inbox_line(line) is None
    m = re.search(r'^(\{"record_id": .*\})$', SKILL, re.M)
    assert m and jobs.RECORD_ID_RE.fullmatch(json.loads(m.group(1).replace("<uuid>", rid))["record_id"])
    assert "scripts/record_id.py --lead <lead_id>" in SKILL


@pytest.mark.parametrize("bad", ["local-0123456789ab", "local-XYZ", "0123456789ab",
                                 "0D7E1A9B-2C3D-5E4F-8A9B-0C1D2E3F4A5B",        # uppercase is not canonical
                                 "0d7e1a9b2c3d5e4f8a9b0c1d2e3f4a5b",            # no hyphens
                                 "0d7e1a9b-2c3d-5e4f-8a9b-0c1d2e3f4a5",         # one digit short
                                 "{0d7e1a9b-2c3d-5e4f-8a9b-0c1d2e3f4a5b}"])
def test_local_prefix_and_malformed_uuids_are_rejected(bad):
    assert jobs.parse_airtable_record(f"AIRTABLE_RECORD: {bad}") is None
    line = {"lead_id": "0123456789ab", "airtable_record_id": bad,
            "completed_at": "2026-10-03T12:00:00+00:00", "source": jobs.INBOX_SOURCE}
    assert jobs.check_inbox_line(line)


def test_the_same_application_always_gets_the_same_uuid5():
    import uuid
    rid = _record_id_mod()
    a = rid.local_record_id(lead_id="0123456789ab")
    assert a == rid.local_record_id(lead_id="0123456789ab") and uuid.UUID(a).version == 5
    assert a != rid.local_record_id(lead_id="0123456789ac")
    assert rid.local_record_id(company="Acme", role="PM") == rid.local_record_id(company="ACME ", role="pm")
    with pytest.raises(ValueError):
        rid.local_record_id(company="Acme")
