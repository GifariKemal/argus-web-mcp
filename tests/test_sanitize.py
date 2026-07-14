"""Guard for the Markdown sanitizer: banned glyphs in, ASCII house style out."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

from sanitize_md import BANNED, excluded, find_offenders, sanitize  # noqa: E402


def test_sanitize_replaces_every_banned_glyph():
    dirty = "a—b, 2014–2015, “q” ‘s’, x→y, 5×3, foo… bar baz"
    clean = sanitize(dirty)
    assert clean == 'a - b, 2014-2015, "q" \'s\', x->y, 5x3, foo... bar baz'
    assert not any(ch in BANNED for ch in clean)


def test_em_dash_normalizes_spacing():
    assert sanitize("word — word") == "word - word"
    assert sanitize("word—word") == "word - word"


def test_clean_text_is_untouched():
    ascii_md = "# Title\n\n- item one\n- item two -> done\n"
    assert sanitize(ascii_md) == ascii_md


def test_find_offenders_reports_line_and_char():
    hits = find_offenders("clean line\nbad—line\n")
    assert hits == [(2, "—", "bad—line")]


def test_gold_and_fixtures_excluded():
    assert excluded(Path("benchmark/gold/docs-01.md"))
    assert excluded(Path("tests/fixtures/x.md"))
    assert not excluded(Path("docs/00-DESIGN.md"))
