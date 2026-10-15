"""Append (or insert) a block into a file only if a marker substring is not already present.

Exists because model-driven sessions in the private system this repo came from appended the
same block 2-5 times to shared files when a step was retried. Every skill that appends to a
shared file (here: /job-apply's terminal completion line and local application records)
calls this instead of `>>`.

Usage:
    python scripts/append_once.py TARGET --marker STR (--text STR | --text-file PATH) [--after LINE]

    --after LINE   insert the block immediately after the first line that equals LINE
                   (e.g. a Markdown table separator row) instead of at the end of the
                   file. Use this for "newest first" tables like Change-Log INDEX.md.

                   Table-anchor guard: if LINE is itself a Markdown table row (e.g. the
                   header row) and the line right after it is a table separator row
                   (cells made only of `-`, `:` and spaces, like `|---|---|`), and the
                   block being inserted also starts with `|`, the insertion point is
                   moved to just after the separator instead — anchoring on a table's
                   header can never split the header from its separator. A short note
                   is printed to stderr when this adjustment happens.

Exit code is always 0. Prints "appended" or "skipped (marker present)". The marker
must occur inside the text being appended, otherwise nothing is written and an error
line goes to stderr.
"""
import argparse
import re
import sys
from pathlib import Path

_TABLE_CELL_RE = re.compile(r":?-+:?")


def _is_table_row(line: str) -> bool:
    """True if `line` looks like a Markdown table row (has cells delimited by `|`)."""
    stripped = line.rstrip("\r\n").strip()
    return stripped.startswith("|") and stripped.count("|") >= 2


def _is_table_separator(line: str) -> bool:
    """True if `line` is a Markdown table separator row, e.g. `|---|:--|--:|`."""
    stripped = line.rstrip("\r\n").strip()
    if not stripped or "-" not in stripped:
        return False
    cells = [c.strip() for c in stripped.strip("|").split("|")]
    return bool(cells) and all(c == "" or _TABLE_CELL_RE.fullmatch(c) for c in cells) and any(cells)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("target")
    ap.add_argument("--marker", required=True, help="unique substring that identifies this block")
    g = ap.add_mutually_exclusive_group(required=True)
    g.add_argument("--text")
    g.add_argument("--text-file")
    ap.add_argument("--after", help="insert after the first line exactly equal to this (default: append at end)")
    a = ap.parse_args()

    block = a.text if a.text is not None else Path(a.text_file).read_text(encoding="utf-8")
    if a.marker not in block:
        print(f"error: marker {a.marker!r} not found in the text to append; nothing written", file=sys.stderr)
        return 0
    if not block.endswith("\n"):
        block += "\n"

    target = Path(a.target)
    existing = target.read_text(encoding="utf-8") if target.exists() else ""
    if a.marker in existing:
        print("skipped (marker present)")
        return 0

    if a.after is not None and existing:
        lines = existing.splitlines(keepends=True)
        for i, line in enumerate(lines):
            if line.rstrip("\r\n") == a.after:
                if not line.endswith("\n"):
                    lines[i] = line + "\n"
                insert_at = i + 1
                if (
                    _is_table_row(line)
                    and i + 1 < len(lines)
                    and _is_table_separator(lines[i + 1])
                    and block.lstrip().startswith("|")
                ):
                    insert_at = i + 2
                    print(
                        "note: --after anchor was a table row followed by a separator; "
                        "inserting after the separator instead to avoid breaking the table",
                        file=sys.stderr,
                    )
                lines.insert(insert_at, block)
                new = "".join(lines)
                break
        else:
            print(f"error: --after line {a.after!r} not found in {target}; nothing written", file=sys.stderr)
            return 0
    else:
        if existing and not existing.endswith("\n"):
            existing += "\n"
        new = existing + block

    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(new, encoding="utf-8")
    print("appended")
    return 0


if __name__ == "__main__":
    sys.exit(main())
