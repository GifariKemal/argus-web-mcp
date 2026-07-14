#!/usr/bin/env python3
"""Sanitize Markdown to the SURIOTA ASCII house style.

Replaces the banned glyphs (em/en-dash, smart quotes, unicode arrows, nbsp,
ellipsis) with plain ASCII. Runs as a pre-commit gate (--check) or a fixer
(--fix). Scope is Markdown only; regenerable/example corpora are skipped.

Usage:
    python scripts/sanitize_md.py --check [paths...]   # exit 1 if dirty
    python scripts/sanitize_md.py --fix   [paths...]   # rewrite in place
No args -> all tracked *.md (via git ls-files), minus EXCLUDE.
"""
from __future__ import annotations

import re
import subprocess
import sys
from pathlib import Path

# Glyph -> ASCII. Order matters only for the regex-based em-dash spacing.
REPLACEMENTS = {
    "‘": "'", "’": "'",            # single smart quotes
    "“": '"', "”": '"',            # double smart quotes
    "–": "-",                            # en-dash
    "…": "...",                          # ellipsis
    " ": " ",                            # non-breaking space
    "→": "->", "←": "<-",           # arrows
    "↔": "<->", "⇒": "=>", "⇐": "<=",
    "•": "-",                            # bullet
    "×": "x",                            # multiplication sign
    "−": "-",                            # minus sign
}
# Em-dash keeps house spacing "a - b" regardless of original padding.
EM_DASH = re.compile(r"\s*—\s*")

# Directories whose Markdown is verbatim example/gold data - never rewrite.
EXCLUDE = ("benchmark/gold/", "tests/fixtures/")

BANNED = set(REPLACEMENTS) | {"—"}


def sanitize(text: str) -> str:
    text = EM_DASH.sub(" - ", text)
    for bad, good in REPLACEMENTS.items():
        text = text.replace(bad, good)
    return text


def excluded(path: Path) -> bool:
    p = path.as_posix()
    return any(x in p for x in EXCLUDE)


def tracked_md() -> list[Path]:
    out = subprocess.run(
        ["git", "ls-files", "*.md"], capture_output=True, text=True, check=True  # noqa: S607
    ).stdout.split()
    return [Path(p) for p in out]


def find_offenders(text: str) -> list[tuple[int, str, str]]:
    hits = []
    for n, line in enumerate(text.splitlines(), 1):
        for ch in line:
            if ch in BANNED:
                hits.append((n, ch, line.strip()[:80]))
                break
    return hits


def main(argv: list[str]) -> int:
    mode = "--check"
    args = []
    for a in argv:
        if a in ("--check", "--fix"):
            mode = a
        else:
            args.append(a)

    paths = [Path(a) for a in args] if args else tracked_md()
    paths = [p for p in paths if p.suffix == ".md" and not excluded(p) and p.exists()]

    dirty = 0
    for path in paths:
        text = path.read_text(encoding="utf-8")
        clean = sanitize(text)
        if clean == text:
            continue
        dirty += 1
        if mode == "--fix":
            path.write_text(clean, encoding="utf-8")
            print(f"fixed  {path.as_posix()}")
        else:
            for n, ch, ctx in find_offenders(text):
                print(f"{path.as_posix()}:{n}: U+{ord(ch):04X} {ch!r}  {ctx}")

    if mode == "--check" and dirty:
        print(f"\n{dirty} file(s) contain banned glyphs. Run: python scripts/sanitize_md.py --fix")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
