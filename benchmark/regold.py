"""Independent gold re-curation helper for the Argus benchmark (P2 Task 8).

PURPOSE / HONESTY: P1 gold was extracted with Argus itself (trafilatura), giving
Argus a home-advantage ROUGE-L of 1.000 - a meaningless gate. This regenerates the
gold for the stable text items with a NEUTRAL THIRD extractor: BeautifulSoup
``.get_text()`` over the page's main-content DOM node. That method is independent
of BOTH adapters under test:
  - Argus pipeline      -> trafilatura  (NOT used here)
  - readability baseline -> readability-lxml  (NOT used here)
BeautifulSoup is a plain DOM-text dumper with no article-detection heuristic, so it
favours no adapter. The resulting gold is rougher than any extractor's output (it
keeps some near-content furniture), which is exactly the point: nobody scores 1.000.

Usage:  ./.venv/Scripts/python.exe benchmark/regold.py
Writes  benchmark/gold/<id>.md  for the items in TARGETS (overwrites P1 gold).
"""

from __future__ import annotations

import re
from pathlib import Path

import httpx
from bs4 import BeautifulSoup

BENCH_DIR = Path(__file__).resolve().parent
GOLD_DIR = BENCH_DIR / "gold"
_UA = "Mozilla/5.0 (compatible; ArgusRegold/0.1; +https://suriota.com)"
_DATE = "2026-06-24"

# Per-item: the neutral main-content node selector. A callable(soup) -> Tag|None.
# Selectors are plain structural DOM nodes - NOT readability/trafilatura heuristics.
TARGETS: dict[str, dict] = {
    "longform-04": {
        "url": "https://www.paulgraham.com/greatwork.html",
        # PG essays are a single <font> block inside the table layout.
        "node": lambda s: s.find("font"),
        "node_desc": "BeautifulSoup .get_text() over the <font> main-text block",
    },
    "docs-01": {
        "url": "https://fastapi.tiangolo.com/tutorial/first-steps/",
        # MkDocs Material renders the doc body in <article>.
        "node": lambda s: s.find("article"),
        "node_desc": "BeautifulSoup .get_text() over the <article> doc body node",
    },
}

# Noise tags to drop before dumping text - pure furniture, present in any DOM and
# removed by every extractor, so dropping them does not bias toward any adapter.
_DROP_TAGS = ("script", "style", "noscript", "nav", "header", "footer")


def _clean(node) -> str:
    """Drop furniture tags + Wikipedia footnote/edit markers, then dump text."""
    for tag in node.find_all(_DROP_TAGS):
        tag.decompose()
    # Wikipedia: superscript ref markers [1] and the [edit] section links are
    # navigation furniture, not prose. Removing them is extractor-neutral.
    for sup in node.find_all("sup", class_="reference"):
        sup.decompose()
    for edit in node.find_all(class_="mw-editsection"):
        edit.decompose()
    text = node.get_text("\n", strip=True)
    # Collapse runs of blank lines; keep paragraph structure.
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


def regold_one(iid: str, spec: dict) -> int:
    url = spec["url"]
    resp = httpx.get(url, follow_redirects=True, timeout=30, headers={"User-Agent": _UA})
    resp.raise_for_status()
    soup = BeautifulSoup(resp.text, "html.parser")
    node = spec["node"](soup)
    if node is None:
        raise RuntimeError(f"{iid}: main-content node not found on {url}")
    body = _clean(node)
    provenance = (
        f"<!-- INDEPENDENT gold (P2 Task 8, re-curated {_DATE}). "
        f"Source: {url} | NEUTRAL method: {spec['node_desc']} "
        f"(bs4 plain DOM text-dump). Independent of Argus (trafilatura) AND of the "
        f"readability baseline - no article-detection heuristic, favours no adapter. -->\n"
    )
    out = GOLD_DIR / f"{iid}.md"
    out.write_text(provenance + body + "\n", encoding="utf-8")
    words = len(body.split())
    print(f"[regold] {iid:<12} {words:>6} words  -> {out.name}")
    return words


def main() -> int:
    GOLD_DIR.mkdir(exist_ok=True)
    for iid, spec in TARGETS.items():
        try:
            regold_one(iid, spec)
        except Exception as exc:  # noqa: BLE001 - report, continue other items
            print(f"[error] {iid}: {exc}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
