"""Offline extraction-quality benchmark on WCXB (Web Content eXtraction Benchmark).

WCXB = 2008 human-reviewed pages, 7 page types, CC-BY-4.0
(https://github.com/Murrough-Foley/web-content-extraction-benchmark). The dataset is
NOT vendored: clone it once into a cache dir outside the repo (~194 MB):

    git clone --depth 1 https://github.com/Murrough-Foley/web-content-extraction-benchmark \
        "%LOCALAPPDATA%/argus-bench/wcxb"

or point ARGUS_WCXB_DIR / --data at an existing clone. Scoring uses the dataset's own
evaluate.py (word-level F1 over \\w+ tokens, plus with/without snippet rates) imported
from that clone, and scorer.quality_f1 with `with` as must_contain and `without` as
must_not_contain. Fully offline: HTML comes from the cached .html.gz files.
"""

from __future__ import annotations

import argparse
import gzip
import json
import os
import random
import statistics
import sys
import time
from collections import defaultdict
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from scorer import quality_f1  # noqa: E402

from argus.extract.article import extract_article  # noqa: E402

_DEFAULT = Path(os.environ.get("LOCALAPPDATA", Path.home() / ".cache")) / "argus-bench" / "wcxb"

# Published dev-set F1 per page type (WCXB README, 1497 pages).
BASELINES = {
    "rs-trafilatura": {
        "article": 0.932,
        "documentation": 0.932,
        "service": 0.844,
        "forum": 0.808,
        "collection": 0.716,
        "listing": 0.707,
        "product": 0.641,
        "overall": 0.859,
    },
    "trafilatura": {
        "article": 0.926,
        "documentation": 0.888,
        "service": 0.763,
        "forum": 0.585,
        "collection": 0.553,
        "listing": 0.589,
        "product": 0.567,
        "overall": 0.791,
    },
    "readability": {
        "article": 0.825,
        "documentation": 0.736,
        "service": 0.604,
        "forum": 0.466,
        "collection": 0.445,
        "listing": 0.496,
        "product": 0.407,
        "overall": 0.675,
    },
}
TYPES = ["article", "documentation", "service", "forum", "collection", "listing", "product"]


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--data", type=Path, default=Path(os.environ.get("ARGUS_WCXB_DIR", _DEFAULT)))
    ap.add_argument("--split", default="dev", choices=["dev", "test"])
    ap.add_argument("--limit", type=int, default=0, help="pages per type (0 = all), seeded sample")
    ap.add_argument(
        "--trafilatura",
        action="store_true",
        help="also score plain trafilatura.extract() on the same pages",
    )
    ap.add_argument("--json", type=Path, help="write per-page rows here")
    a = ap.parse_args()

    if not (a.data / "evaluate.py").exists():
        sys.exit(f"WCXB not found at {a.data}; clone it there (see module docstring)")
    sys.path.insert(0, str(a.data))
    # evaluate.load_ground_truth opens JSON in the locale codec (cp1252 on Windows), so
    # load it here as UTF-8; page type and metric still come from the dataset's evaluate.py.
    from evaluate import get_page_type, snippet_check, word_f1

    gt: dict[str, dict] = {}
    by_type: dict[str, list[str]] = defaultdict(list)
    for f in sorted((a.data / a.split / "ground-truth").glob("*.json")):
        d = json.loads(f.read_text(encoding="utf-8"))
        g = d.get("ground_truth") or {}
        gt[f.stem] = {
            "url": d.get("url", ""),
            "main_content": g.get("main_content") or "",
            "with": g.get("with") or [],
            "without": g.get("without") or [],
        }
        by_type[get_page_type(d)].append(f.stem)
    if a.limit:
        rng = random.Random(0)  # noqa: S311 - deterministic sample, not crypto
        by_type = {t: sorted(rng.sample(ids, min(a.limit, len(ids)))) for t, ids in by_type.items()}

    if a.trafilatura:
        import trafilatura
    rows = []
    for t in sorted(by_type):
        for fid in by_type[t]:
            g = gt[fid]
            html = gzip.open(
                a.data / a.split / "html" / f"{fid}.html.gz",
                "rt",
                encoding="utf-8",
                errors="replace",
            ).read()
            url = g["url"]
            row = {"id": fid, "type": t, "error": None}
            t0 = time.perf_counter()
            try:
                pred = extract_article(html, url)["content"]
            except Exception as e:  # noqa: BLE001 - record and keep going
                pred, row["error"] = "", f"{type(e).__name__}: {e}"[:200]
            row["secs"] = time.perf_counter() - t0
            row["p"], row["r"], row["f1"] = word_f1(pred, g["main_content"])
            row["with"] = snippet_check(pred, g["with"])
            row["without"] = snippet_check(pred, g["without"])
            row["qf1"] = quality_f1(pred, g["with"], g["without"])
            if a.trafilatura:
                tp = trafilatura.extract(html, url=url) or ""
                row["traf_f1"] = word_f1(tp, g["main_content"])[2]
            rows.append(row)
            print(f"\r{len(rows)} pages", end="", file=sys.stderr, flush=True)
    print(file=sys.stderr)

    def line(name: str, rs: list[dict]) -> str:
        f = [r["f1"] for r in rs]
        cells = [
            name,
            len(rs),
            f"{statistics.mean(f):.3f}",
            f"{statistics.median(f):.3f}",
            f"{statistics.mean(r['p'] for r in rs):.3f}",
            f"{statistics.mean(r['r'] for r in rs):.3f}",
            f"{statistics.mean(r['qf1'] for r in rs):.3f}",
            f"{statistics.mean(r['with'] for r in rs):.1%}",
            f"{statistics.mean(r['without'] for r in rs):.1%}",
            f"{1000 * statistics.mean(r['secs'] for r in rs):.0f}",
        ]
        if a.trafilatura:
            cells.append(f"{statistics.mean(r['traf_f1'] for r in rs):.3f}")
        key = name.strip("*")
        cells += [f"{BASELINES[b][key]:.3f}" for b in ("trafilatura", "rs-trafilatura")]
        return "| " + " | ".join(map(str, cells)) + " |"

    head = (
        ["type", "n", "F1 mean", "F1 median", "P", "R", "quality_f1", "with", "without", "ms/page"]
        + (["traf F1 (same pages)"] if a.trafilatura else [])
        + ["pub. trafilatura", "pub. rs-traf"]
    )
    print(f"WCXB {a.split}, {len(rows)} pages, Argus extract_article (markdown)\n")
    print("| " + " | ".join(head) + " |\n|" + "---|" * len(head))
    for t in [t for t in TYPES if any(r["type"] == t for r in rows)]:
        print(line(t, [r for r in rows if r["type"] == t]))
    print(line("**overall**", rows))
    print(
        "\n`with` = required snippets found (higher better); `without` = boilerplate "
        "snippets leaked (lower better). Published numbers are the full dev set."
    )

    print("\nSlowest 10:")
    for r in sorted(rows, key=lambda r: -r["secs"])[:10]:
        print(f"  {r['id']} {r['type']:<13} {r['secs']:.2f}s f1={r['f1']:.3f}")
    print("\nWorst F1 10:")
    for r in sorted(rows, key=lambda r: r["f1"])[:10]:
        print(f"  {r['id']} {r['type']:<13} f1={r['f1']:.3f} p={r['p']:.3f} r={r['r']:.3f}")
    errs = [r for r in rows if r["error"]]
    print(f"\nExceptions: {len(errs)}")
    for r in errs:
        print(f"  {r['id']} {r['type']}: {r['error']}")
    if a.json:
        a.json.write_text(json.dumps(rows, indent=1), encoding="utf-8")


if __name__ == "__main__":
    main()
