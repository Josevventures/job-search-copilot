"""Text hygiene for strings copied from job pages (addendum §3: untrusted text, stored only as data). Stdlib only."""
import re
import unicodedata

_DROP_CATEGORIES = ("Cf", "Cs", "Co", "Cn")  # format (zero-width, bidi, soft hyphen), surrogate, private, unassigned
_NUM = r"(\d[\d,]*)"
_CHARS = r"(?:characters?|chars?)"
_WORDS = r"words?"
_LEAD = r"(?:max(?:imum)?|up to|limit(?:ed)?(?: of| to)?|no more than|at most|under)\s*:?\s*"
_TRAIL = r"\s*(?:max(?:imum)?|limit|or (?:less|fewer))"
_CHARS_RE = re.compile(rf"{_LEAD}{_NUM}\s*{_CHARS}\b|{_NUM}\s*{_CHARS}{_TRAIL}", re.I)
_WORDS_RE = re.compile(rf"{_LEAD}{_NUM}\s*{_WORDS}\b|{_NUM}\s*{_WORDS}{_TRAIL}", re.I)


def _is_invisible(cp: int) -> bool:
    """Variation selectors and the combining grapheme joiner: category Mn, but invisible and spoofable."""
    return 0xFE00 <= cp <= 0xFE0F or 0xE0100 <= cp <= 0xE01EF or cp == 0x034F


def clean(value, cap: int = 500) -> str | None:
    """NFKC, drop control/format/zero-width characters, collapse whitespace, cap the length. Never raises."""
    if value is None:
        return None
    try:
        s = str(value)[: cap * 4]  # bound the work before NFKC; huge inputs cannot stall the extractor
        s = unicodedata.normalize("NFKC", s)
    except Exception:
        return ""
    out = []
    for ch in s:
        cat = unicodedata.category(ch)
        if cat in ("Cc", "Zl", "Zp"):
            out.append(" ")  # separators become spaces, not deletions, so words do not fuse
        elif cat in _DROP_CATEGORIES or _is_invisible(ord(ch)):
            continue
        else:
            out.append(ch)
    return re.sub(r"\s+", " ", "".join(out)).strip()[:cap]


def norm_label(value) -> str:
    """The form the click rules compare: clean(), lowercased."""
    return (clean(value) or "").lower()


def _first_int(m) -> int | None:
    if not m:
        return None
    raw = next(g for g in m.groups() if g)
    return int(raw.replace(",", ""))


def parse_limits(text) -> tuple[int | None, int | None]:
    """(max_chars, max_words) from text like 'max 150 words' or '2,000 characters max'."""
    s = clean(text, 2000) or ""
    return _first_int(_CHARS_RE.search(s)), _first_int(_WORDS_RE.search(s))
