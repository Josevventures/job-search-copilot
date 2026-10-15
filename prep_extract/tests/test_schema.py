import json

from prep_extract import schema

SPEC_KEYS = {"schema_version", "url", "ats", "adapter", "extracted_at", "questions_visible", "sign_in_required",
             "pages", "cover_letter", "resume", "click_log", "unseen_pages", "errors"}


def test_new_result_has_exactly_the_spec_keys_and_safe_defaults():
    r = schema.new_result("https://jobs.example.com/a")
    assert set(r) == SPEC_KEYS
    assert r["schema_version"] == 1 and r["questions_visible"] is False and r["sign_in_required"] is False
    assert r["pages"] == [] and r["click_log"] == [] and r["errors"] == [] and r["unseen_pages"] is False
    assert r["cover_letter"] == {"mode": "none", "required": False}
    assert r["resume"] == {"present": False, "required": False}
    assert r["extracted_at"].endswith("+00:00")


def test_make_field_sanitizes_and_reads_limits():
    f = schema.make_field({"label": "Why  Acme?\u200b *", "type": "textarea", "required": False,
                           "help_text": "Maximum 1,000 characters", "options": None})
    assert f == {"label": "Why Acme?", "type": "textarea", "required": True, "max_chars": 1000,
                 "max_words": None, "options": [], "help_text": "Maximum 1,000 characters"}


def test_make_field_prefers_maxlength_and_handles_heavy_asterisk():
    f = schema.make_field({"label": "Full name\u2731", "type": "text", "maxlength": 80, "help_text": "max 50 chars"})
    assert f["label"] == "Full name" and f["required"] is True and f["max_chars"] == 80


def test_make_field_rejects_unknown_types_and_caps_options():
    f = schema.make_field({"label": "Pick", "type": "<script>", "options": ["o" * 300] + [str(i) for i in range(150)]})
    assert f["type"] == "text"
    assert len(f["options"]) == 100 and f["options"][0] == "o" * 200
    assert schema.make_field({"label": "x", "options": "not a list"})["options"] == []
    assert schema.make_field({"label": "x", "maxlength": True})["max_chars"] is None


def test_add_page_caps_fields_at_200_with_one_error():
    r = schema.new_result("u")
    schema.add_page(r, 1, [{"label": f"Q{i}", "type": "text"} for i in range(150)])
    schema.add_page(r, 2, [{"label": f"R{i}", "type": "text"} for i in range(150)])
    assert schema.field_count(r) == 200 and len(r["pages"][1]["fields"]) == 50
    assert r["errors"] == ["field limit reached (200); remaining fields dropped"]


def test_add_click_and_errors_are_capped_and_sanitized():
    r = schema.new_result("u")
    for i in range(150):
        schema.add_click(r, 1, "Sub\u200bmit " + "x" * 300, "deny", "label contains 'submit'")
        schema.add_error(r, f"e{i}\u0007")
    assert len(r["click_log"]) == 100 and len(r["errors"]) == 20
    row = r["click_log"][0]
    assert row["page"] == 1 and row["decision"] == "deny" and row["label"].startswith("Submit x") and len(row["label"]) == 200
    assert r["errors"][0] == "e0"


def test_finalize_detects_cover_letter_and_resume():
    r = schema.new_result("u")
    schema.add_page(r, 1, [{"label": "Resume/CV", "type": "file", "required": True},
                           {"label": "Cover Letter", "type": "file", "required": False}])
    r = schema.finalize(r)
    assert r["questions_visible"] is True
    assert r["cover_letter"] == {"mode": "upload", "required": False}
    assert r["resume"] == {"present": True, "required": True}


def test_finalize_text_cover_letter_and_sign_in_hides_questions():
    r = schema.new_result("u")
    schema.add_page(r, 1, [{"label": "Cover letter", "type": "textarea", "required": True}])
    r["sign_in_required"] = True
    r = schema.finalize(r)
    assert r["cover_letter"] == {"mode": "text", "required": True}
    assert r["questions_visible"] is False


def test_stub_result_is_a_valid_empty_form():
    r = schema.stub_result("https://jobs.example.com/a", "extractor failed: timed out\u0000")
    assert r["questions_visible"] is False and r["errors"] == ["extractor failed: timed out"]
    assert json.loads(json.dumps(r, ensure_ascii=False)) == r


def test_add_click_and_add_page_survive_garbage_page_numbers_and_cap_decision():
    r = schema.new_result("u")
    schema.add_click(r, None, "Next", "d" * 200 + "​", "why")
    schema.add_click(r, "garbage", "Next", "allow", "why")
    schema.add_page(r, None, [{"label": "Q"}])
    assert [c["page"] for c in r["click_log"]] == [0, 0] and len(r["click_log"][0]["decision"]) == 50
    assert r["pages"][0]["page"] == 0


def test_limits_are_capped():
    f = schema.make_field({"label": "x", "maxlength": 10**30, "help_text": "max " + "9" * 40 + " words"})
    assert f["max_chars"] == 1_000_000 and f["max_words"] == 1_000_000


def test_make_field_maps_a_mid_label_heavy_asterisk_to_required_and_strips_it():
    # Lever wraps the whole card in one <label>: "Resume/CV ✱ ATTACH RESUME/CV" (seen in acceptance 2026-10-15)
    f = schema.make_field({"label": "Resume/CV ✱ ATTACH RESUME/CV", "type": "file", "required": False})
    assert f["required"] is True
    assert "✱" not in f["label"] and f["label"] == "Resume/CV ATTACH RESUME/CV"


def test_make_field_leaves_an_inline_ascii_star_alone():
    f = schema.make_field({"label": "Rate 5* experiences", "type": "text"})
    assert f["required"] is False and f["label"] == "Rate 5* experiences"


def test_make_field_marks_a_missing_label_explicitly():
    f = schema.make_field({"label": "", "type": "radio", "options": ["Yes", "No"]})
    assert f["label"] == "" and f["label_missing"] is True
    g = schema.make_field({"label": "​✱ ", "type": "text"})   # only invisible chars and a marker
    assert g["label"] == "" and g["label_missing"] is True and g["required"] is True
    assert "label_missing" not in schema.make_field({"label": "Name", "type": "text"})
