"""Offline embedding-model eval for the semantic rerank (Indonesian + English).

Embeds semantic_id.yaml exactly like Argus does (plain query, "<title> <snippet>" docs,
batch_size=8, threads=2), then reports per language: mean relevant / irrelevant cosine,
margin (min rel - max irr, per query, averaged), pairwise AUC, nDCG@3, plus load RSS and
embedding time. Each model runs in its own subprocess so RSS is not polluted by the others.
Finally it suggests _SEM_FLOOR / _SEM_GUARD_FLOOR from the irrelevant-score distribution.

    ./.venv/Scripts/python.exe benchmark/semantic_id.py            # all candidates
    ./.venv/Scripts/python.exe benchmark/semantic_id.py --models BAAI/bge-small-en-v1.5 \
        sentence-transformers/paraphrase-multilingual-MiniLM-L12-v2 --json out.json
"""

import argparse
import json
import math
import os
import subprocess
import sys
import time
from pathlib import Path

import numpy as np
import yaml

DATA = Path(__file__).with_name("semantic_id.yaml")
MODELS = [
    "BAAI/bge-small-en-v1.5",  # current Argus model
    "sentence-transformers/paraphrase-multilingual-MiniLM-L12-v2",
    "minishlab/potion-multilingual-128M",  # static (model2vec) multilingual, ~0.5 GB
]
CURRENT = (0.30, 0.55)  # src/argus/search.py _SEM_FLOOR, _SEM_GUARD_FLOOR
IDCG3 = sum(1 / math.log2(i + 2) for i in range(3))


def _rss_mb() -> float:
    import psutil

    return psutil.Process().memory_info().rss / 2**20


def measure(model: str) -> dict:
    """Child-process body: load, embed everything, return raw cosines + costs."""
    queries = yaml.safe_load(DATA.read_text(encoding="utf-8"))["queries"]
    base = _rss_mb()
    from fastembed import TextEmbedding

    t0 = time.perf_counter()
    emb = TextEmbedding(model_name=model, threads=2)
    list(emb.embed(["warm"], batch_size=8))
    load_s, load_rss = time.perf_counter() - t0, _rss_mb()
    texts = [t for q in queries for t in (q["query"], *q["relevant"], *q["irrelevant"])]
    t0 = time.perf_counter()
    vecs = np.array(list(emb.embed(texts, batch_size=8)), dtype=np.float64)
    embed_s = time.perf_counter() - t0
    vecs /= np.linalg.norm(vecs, axis=1, keepdims=True)
    rows, i = [], 0
    for q in queries:
        sims = (vecs[i + 1 : i + 7] @ vecs[i]).tolist()
        rows.append({"id": q["id"], "lang": q["lang"], "rel": sims[:3], "irr": sims[3:]})
        i += 7
    return {
        "model": model,
        "texts": len(texts),
        "base_rss_mb": base,
        "load_rss_mb": load_rss,
        "peak_rss_mb": _rss_mb(),
        "load_s": load_s,
        "embed_s": embed_s,
        "rows": rows,
    }


def metrics(rows: list[dict]) -> dict:
    """Aggregate per-query cosines into the reported metrics."""
    auc, margin, ndcg = [], [], []
    for r in rows:
        auc.append(np.mean([(a > b) + 0.5 * (a == b) for a in r["rel"] for b in r["irr"]]))
        margin.append(min(r["rel"]) - max(r["irr"]))
        top3 = sorted([(s, 1) for s in r["rel"]] + [(s, 0) for s in r["irr"]], reverse=True)[:3]
        ndcg.append(sum(g / math.log2(k + 2) for k, (_, g) in enumerate(top3)) / IDCG3)
    return {
        "n": len(rows),
        "rel": np.mean([s for r in rows for s in r["rel"]]),
        "irr": np.mean([s for r in rows for s in r["irr"]]),
        "margin": np.mean(margin),
        "auc": np.mean(auc),
        "ndcg3": np.mean(ndcg),
    }


def rates(rows: list[dict], t: float) -> tuple[float, float]:
    """(fraction of irrelevant rejected, fraction of relevant kept) at cosine floor t."""
    irr = [s for r in rows for s in r["irr"]]
    rel = [s for r in rows for s in r["rel"]]
    return np.mean([s < t for s in irr]), np.mean([s >= t for s in rel])


def suggest(rows: list[dict]) -> tuple[float, float]:
    """Floors from the score distributions, per language, then the stricter language.

    _SEM_FLOOR decides whether a zero-lexical-overlap row survives, so losing a relevant
    row is the costly error: floor = 5th percentile of RELEVANT cosine (keeps ~95% of
    relevant in both languages), rounded DOWN to 0.05. _SEM_GUARD_FLOOR decides whether a
    row counts as on-topic for the off-topic guard, so a false "relevant" is the costly
    error: guard = 95th percentile of IRRELEVANT cosine, rounded UP to 0.05.
    """
    langs = ("id", "en")
    rel = [np.percentile([s for r in rows if r["lang"] == g for s in r["rel"]], 5) for g in langs]
    irr = [np.percentile([s for r in rows if r["lang"] == g for s in r["irr"]], 95) for g in langs]
    return math.floor(min(rel) * 20 + 1e-9) / 20, math.ceil(max(irr) * 20 - 1e-9) / 20


def report(results: list[dict]) -> None:
    print("| model | lang | n | rel cos | irr cos | margin | AUC | nDCG@3 |")
    print("|---|---|---|---|---|---|---|---|")
    for res in results:
        for lang in ("id", "en"):
            m = metrics([r for r in res["rows"] if r["lang"] == lang])
            print(
                f"| {res['model']} | {lang} | {m['n']} | {m['rel']:.3f} | {m['irr']:.3f} "
                f"| {m['margin']:+.3f} | {m['auc']:.3f} | {m['ndcg3']:.3f} |"
            )
    print("\n| model | load s | load RSS delta MB | peak RSS MB | embed s (texts) |")
    print("|---|---|---|---|---|")
    for res in results:
        print(
            f"| {res['model']} | {res['load_s']:.1f} "
            f"| {res['load_rss_mb'] - res['base_rss_mb']:.0f} "
            f"| {res['peak_rss_mb']:.0f} | {res['embed_s']:.2f} ({res['texts']}) |"
        )
    print("\nFloors: irr rejected / rel kept, per language (id | en)")
    print("| model | which | floor | guard | floor id | floor en | guard id | guard en |")
    print("|---|---|---|---|---|---|---|---|")
    for res in results:
        for which, (f, g) in (("current", CURRENT), ("suggested", suggest(res["rows"]))):
            cells = [
                "{:.0%} / {:.0%}".format(*rates([r for r in res["rows"] if r["lang"] == lang], t))
                for t in (f, g)
                for lang in ("id", "en")
            ]
            print(f"| {res['model']} | {which} | {f:.2f} | {g:.2f} | " + " | ".join(cells) + " |")
    print("\nSweep: irr rejected / rel kept at each cosine floor (id | en)")
    ts = [round(0.25 + 0.05 * k, 2) for k in range(12)]
    print("| model | lang | " + " | ".join(f"{t:.2f}" for t in ts) + " |")
    print("|---|---|" + "---|" * len(ts))
    for res in results:
        for lang in ("id", "en"):
            sub = [r for r in res["rows"] if r["lang"] == lang]
            cells = ["{:.0%}/{:.0%}".format(*rates(sub, t)) for t in ts]
            print(f"| {res['model'].split('/')[-1]} | {lang} | " + " | ".join(cells) + " |")


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--models", nargs="+", default=MODELS)
    ap.add_argument("--child", help=argparse.SUPPRESS)
    ap.add_argument("--json", type=Path, help="also dump raw per-query cosines here")
    args = ap.parse_args()
    if args.child:
        print(json.dumps(measure(args.child)))
        return
    perfect = metrics([{"rel": [0.9, 0.8, 0.7], "irr": [0.3, 0.2, 0.1]}])
    assert perfect["auc"] == 1 and abs(perfect["ndcg3"] - 1) < 1e-9 and perfect["margin"] > 0
    env = {**os.environ, "OMP_NUM_THREADS": "2"}
    results = []
    for model in args.models:
        out = subprocess.run(  # noqa: S603 - argv is our own interpreter + model id
            [sys.executable, __file__, "--child", model],
            capture_output=True,
            text=True,
            check=True,
            env=env,
        ).stdout
        results.append(json.loads(out.strip().splitlines()[-1]))
    if args.json:
        args.json.write_text(json.dumps(results, indent=1), encoding="utf-8")
    report(results)


if __name__ == "__main__":
    main()
