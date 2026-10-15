import pytest

from prep_extract import sanitize


def test_clean_strips_controls_zero_width_and_bidi():
    raw = "Sub\u200bmit\u0007 app\u202elication\ufeff\u00ad"
    assert sanitize.clean(raw) == "Submit application"


def test_clean_applies_nfkc_and_collapses_whitespace():
    assert sanitize.clean("  \uff33\uff55\uff42\uff4d\uff49\uff54 \n\t now  ") == "Submit now"


def test_clean_caps_and_passes_none_through():
    assert sanitize.clean("x" * 1000) == "x" * 500
    assert sanitize.clean("abcdef", cap=3) == "abc"
    assert sanitize.clean(None) is None
    assert sanitize.clean(123) == "123"


def test_norm_label_lowercases_and_handles_none():
    assert sanitize.norm_label("  Save &  Continue ") == "save & continue"
    assert sanitize.norm_label(None) == ""


@pytest.mark.parametrize("text,expected", [
    ("Why us? (max 150 words)", (None, 150)),
    ("Maximum 1,000 characters", (1000, None)),
    ("up to 500 chars", (500, None)),
    ("2000 characters max", (2000, None)),
    ("Please keep it under 300 words maximum", (None, 300)),
    ("No more than 250 words", (None, 250)),
    ("Limit: 400 characters", (400, None)),
    ("Tell us in 3 sentences", (None, None)),
    ("", (None, None)),
    (None, (None, None)),
])
def test_parse_limits(text, expected):
    assert sanitize.parse_limits(text) == expected


class _Hostile:
    def __str__(self):
        raise ValueError("boom")


@pytest.mark.parametrize("value,expected", [
    (_Hostile(), ""), (b"bytes", "b'bytes'"), ({"a": 1}, "{'a': 1}"), (["x"], "['x']"), (True, "True")])
def test_clean_never_raises(value, expected):
    assert sanitize.clean(value) == expected


def test_clean_maps_odd_controls_and_line_separators_to_space():
    assert sanitize.clean("a\x0bb\x85c") == "a b c"
    assert sanitize.clean("a b c\x0cd") == "a b c d"


def test_clean_strips_variation_selectors_and_cgj():
    assert sanitize.clean("a️b\U000e0100c͏d") == "abcd"


def test_clean_bounds_huge_input_quickly():
    import time
    t = time.perf_counter()
    out = sanitize.clean("x" * 5_000_000)
    assert out == "x" * 500 and time.perf_counter() - t < 0.5
