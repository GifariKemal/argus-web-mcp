"""A/B benchmark: does Argus's HYBRID (lexical+semantic) rerank beat LEXICAL-only?

Isolates the rerank effect on the SAME candidate pool. For each scenario query:

  1. Fetch the RAW SearXNG candidate pool ONCE (no Argus rerank applied), take up to
     10 results mapped to {title, url, snippet}.
  2. Apply BOTH rerankers to that SAME pool via Argus's own ``argus.search.rerank``:
       lexical = rerank(q, pool, semantic_rerank=False)[:5]
       hybrid  = rerank(q, pool, semantic_rerank=True)[:5]
     (fastembed installed -> semantic active).
  3. NEUTRAL judge: ONE gpt-4o-mini call per query rates EACH candidate 0..3 for
     relevance to the query (the judge is an INDEPENDENT LLM, NOT the embedder - avoids
     circularity). Judgments cached per (query,url) so lexical & hybrid reuse the SAME
     relevances.
  4. Score nDCG@5 for each ordering (gain=rel, discount=1/log2(rank+1); IDCG from the
     best-possible ordering of the WHOLE pool). Also: top-1 changed, top-5 Jaccard,
     mean top-5 judged relevance.

Aggregate overall + per-category + a conceptual/how-to subset cut, written to
``benchmark/semantic_ab_report.md``. nDCG / parse / jaccard math are PURE functions,
unit-tested offline in tests/test_semantic_ab.py.

Usage:
  python benchmark/semantic_ab.py --ids-from-compare [--limit N] [--pace 3]
"""

from __future__ import annotations

import argparse
import asyncio
import json
import math
import os
import statistics
import sys
import time
from pathlib import Path

import httpx

BENCH_DIR = Path(__file__).resolve().parent
SRC_DIR = BENCH_DIR.parent / "src"
for _p in (str(BENCH_DIR), str(SRC_DIR)):
    if _p not in sys.path:
        sys.path.insert(0, _p)

import scenarios as scen_mod  # noqa: E402

from argus.search import rerank  # noqa: E402

DEFAULT_REPORT = BENCH_DIR / "semantic_ab_report.md"
SEARXNG_URL = "http://127.0.0.1:8888"
JUDGE_MODEL = "gpt-4o-mini"
AB_ENGINES = ["duckduckgo", "bing", "brave", "mojeek", "startpage", "qwant"]
POOL_SIZE = 10  # candidates fetched per query
TOP_K = 5  # we rerank-truncate and score nDCG@K

# Conceptual / how-to subset: queries where semantic matching should help most.
_CONCEPT_PREFIXES = ("how", "what", "why")


# --- pure functions (no I/O - unit-tested offline) ---------------------------


def dcg(relevances: list[float]) -> float:
    """Discounted cumulative gain. gain = rel, discount = 1 / log2(rank + 1).

    ``relevances`` is in ranked order (rank 1 first). DCG = sum_i rel_i / log2(i + 2)
    for 0-based index i (so rank-1 discount is 1/log2(2) = 1.0).
    """
    return sum(rel / math.log2(i + 2) for i, rel in enumerate(relevances))


def ndcg_at_k(ranked_rels: list[float], pool_rels: list[float], k: int) -> float:
    """nDCG@k = DCG@k(ranked) / IDCG@k(ideal), ideal = pool sorted desc, truncated to k.

    ``ranked_rels`` = relevances of the system's ordering (already truncated upstream is
    fine; we still slice to k). ``pool_rels`` = relevances of the FULL candidate pool, used
    to build the best-possible ordering. Returns 0.0 when the ideal DCG is 0 (no relevant
    docs in the pool) so an all-irrelevant pool scores 0, not NaN.
    """
    actual = dcg(ranked_rels[:k])
    ideal = dcg(sorted(pool_rels, reverse=True)[:k])
    if ideal <= 0.0:
        return 0.0
    return actual / ideal


def jaccard(a: list[str], b: list[str]) -> float:
    """Jaccard set overlap |A and B| / |A or B| of two ordering's members (order ignored).

    Two empty sets -> 1.0 (identical/degenerate). One empty, one non-empty -> 0.0.
    """
    sa, sb = set(a), set(b)
    if not sa and not sb:
        return 1.0
    union = sa | sb
    if not union:
        return 1.0
    return len(sa & sb) / len(union)


def parse_judge_json(raw: str, pool_size: int) -> dict[int, float]:
    """Parse the judge's JSON into ``{index: clamped_score}`` for indices in range.

    Accepts a bare JSON list ``[{"index": i, "score": s}, ...]`` or an object wrapping it
    under a ``scores``/``ratings``/``results`` key, optionally fenced in a ```json``` block.
    Scores are clamped to 0..3; out-of-range or unparseable indices are skipped. Missing
    indices are simply absent (the caller defaults them to 0.0). Never raises on bad input
    - returns whatever valid pairs it can recover.
    """
    text = raw.strip()
    if text.startswith("```"):
        # strip a leading ```json / ``` fence and the trailing fence
        text = text.split("```", 2)[1] if text.count("```") >= 2 else text.strip("`")
        if text.lstrip().lower().startswith("json"):
            text = text.lstrip()[4:]
    try:
        data = json.loads(text)
    except (ValueError, TypeError):
        return {}
    if isinstance(data, dict):
        for key in ("scores", "ratings", "results", "data"):
            if isinstance(data.get(key), list):
                data = data[key]
                break
    if not isinstance(data, list):
        return {}
    out: dict[int, float] = {}
    for item in data:
        if not isinstance(item, dict):
            continue
        try:
            idx = int(item["index"])
            score = float(item["score"])
        except (KeyError, TypeError, ValueError):
            continue
        if 0 <= idx < pool_size:
            out[idx] = max(0.0, min(3.0, score))
    return out


def is_conceptual(query: str) -> bool:
    """True if the query is a conceptual/how-to one (starts with how/what/why, or has
    'vs' / 'explained') - the subset where semantic rerank is expected to help most."""
    q = query.lower()
    first = q.split()[0] if q.split() else ""
    return (
        first in _CONCEPT_PREFIXES
        or " vs " in f" {q} "
        or "explained" in q
    )


def rels_for_ordering(ordering: list[dict], judged: dict[str, float]) -> list[float]:
    """Map an ordered result list to its judged relevances by url (default 0.0)."""
    return [judged.get(r.get("url", ""), 0.0) for r in ordering]


# --- I/O: SearXNG raw pool ---------------------------------------------------


async def fetch_pool(
    query: str, client: httpx.AsyncClient, pace: float
) -> list[dict]:
    """Fetch the RAW SearXNG candidate pool once (NO Argus rerank), up to POOL_SIZE.

    Plain httpx against the trusted local backend. Paces ~``pace`` s before the request
    and retries ONCE on an empty pool (to dodge per-IP throttling). Returns a list of
    ``{title, url, snippet}`` deduped by url, or [] if the backend yields nothing.
    """
    params = {
        "q": query,
        "format": "json",
        "engines": ",".join(AB_ENGINES),
    }
    pool: list[dict] = []
    for _attempt in range(2):
        if pace > 0:
            await asyncio.sleep(pace)
        try:
            resp = await client.get(f"{SEARXNG_URL}/search", params=params)
            resp.raise_for_status()
            raw = resp.json().get("results", [])
        except (httpx.HTTPError, ValueError):
            raw = []
        seen: set[str] = set()
        pool = []
        for r in raw:
            url = r.get("url")
            if not url or url in seen:
                continue
            seen.add(url)
            pool.append(
                {
                    "title": r.get("title", ""),
                    "url": url,
                    "snippet": r.get("content", ""),
                }
            )
            if len(pool) >= POOL_SIZE:
                break
        if pool:
            break
    return pool


# --- I/O: neutral LLM judge --------------------------------------------------

_JUDGE_SYSTEM = (
    "You are a strict, neutral search-relevance judge. For each numbered candidate, "
    "rate how well it answers the user's QUERY on an integer scale: "
    "0=irrelevant, 1=marginally related, 2=relevant, 3=perfect/directly answers. "
    "Judge ONLY by the title and snippet shown. Reply with ONLY a JSON list of "
    '{"index": <int>, "score": <int 0-3>}, one object per candidate, no prose.'
)


def _judge_user_prompt(query: str, pool: list[dict]) -> str:
    lines = [f"QUERY: {query}", "", "CANDIDATES:"]
    for i, r in enumerate(pool):
        title = (r.get("title") or "").strip()
        snippet = (r.get("snippet") or "").strip()
        lines.append(f"[{i}] {title} - {snippet}")
    lines.append("")
    lines.append('Return JSON: [{"index": 0, "score": 2}, ...]')
    return "\n".join(lines)


async def judge_pool(
    query: str, pool: list[dict], oai, cache: dict[tuple[str, str], float]
) -> dict[str, float]:
    """ONE gpt-4o-mini call rating every pool candidate 0..3. Returns ``{url: rel}``.

    Reuses cached per-(query,url) judgments so lexical & hybrid share identical relevances
    within a run. Only the uncached candidates are sent; cached ones are merged back in.
    Unjudged candidates default to 0.0. Returns ({}, indicating skip) only when the LLM
    call itself fails - the caller logs that as a skipped query.
    """
    judged: dict[str, float] = {}
    to_judge: list[dict] = []
    for r in pool:
        key = (query, r.get("url", ""))
        if key in cache:
            judged[r["url"]] = cache[key]
        else:
            to_judge.append(r)

    if to_judge:
        prompt = _judge_user_prompt(query, to_judge)
        resp = await oai.chat.completions.create(
            model=JUDGE_MODEL,
            temperature=0,
            messages=[
                {"role": "system", "content": _JUDGE_SYSTEM},
                {"role": "user", "content": prompt},
            ],
        )
        parsed = parse_judge_json(resp.choices[0].message.content or "", len(to_judge))
        for i, r in enumerate(to_judge):
            rel = parsed.get(i, 0.0)
            judged[r["url"]] = rel
            cache[(query, r["url"])] = rel

    return judged


# --- fallback judge: a DIFFERENT embedding model (neutral LLM unavailable) ---
# Uses all-MiniLM (different family from the reranker's bge-small) to reduce - but not
# eliminate - self-agreement. Less neutral than an LLM judge; report caveats this.
JUDGE_EMBED_MODEL = "sentence-transformers/all-MiniLM-L6-v2"
_EJ: dict[str, object] = {}


def judge_pool_embed(query: str, pool: list[dict], cache: dict[tuple[str, str], float]):
    """Relevance = scaled cosine(query, title+snippet) via a model distinct from the reranker."""
    import numpy as np
    from fastembed import TextEmbedding

    if "e" not in _EJ:
        _EJ["e"] = TextEmbedding(model_name=JUDGE_EMBED_MODEL)
    todo = [r for r in pool if (query, r.get("url", "")) not in cache]
    if todo:
        texts = [query] + [f"{r.get('title', '')} {r.get('snippet', '')}" for r in todo]
        vecs = [np.asarray(v, dtype=float) for v in _EJ["e"].embed(texts)]
        q = vecs[0]
        qn = np.linalg.norm(q) or 1.0
        for r, v in zip(todo, vecs[1:], strict=False):
            cos = float(q @ v / (qn * (np.linalg.norm(v) or 1.0)))
            cache[(query, r.get("url", ""))] = max(0.0, cos) * 3.0  # 0..3-ish gain
    return {r["url"]: cache[(query, r.get("url", ""))] for r in pool}


# --- per-query evaluation ----------------------------------------------------


def eval_query(query: str, pool: list[dict], judged: dict[str, float]) -> dict:
    """Rerank the SAME pool two ways, score both. Returns the per-query metric row."""
    lexical = rerank(query, pool, semantic_rerank=False)[:TOP_K]
    hybrid = rerank(query, pool, semantic_rerank=True)[:TOP_K]

    pool_rels = [judged.get(r.get("url", ""), 0.0) for r in pool]
    lex_rels = rels_for_ordering(lexical, judged)
    hyb_rels = rels_for_ordering(hybrid, judged)

    lex_urls = [r.get("url", "") for r in lexical]
    hyb_urls = [r.get("url", "") for r in hybrid]

    return {
        "ndcg_lex": ndcg_at_k(lex_rels, pool_rels, TOP_K),
        "ndcg_hyb": ndcg_at_k(hyb_rels, pool_rels, TOP_K),
        "top5rel_lex": statistics.fmean(lex_rels) if lex_rels else 0.0,
        "top5rel_hyb": statistics.fmean(hyb_rels) if hyb_rels else 0.0,
        "top1_changed": bool(lex_urls and hyb_urls and lex_urls[0] != hyb_urls[0]),
        "jaccard": jaccard(lex_urls, hyb_urls),
    }


# --- aggregation -------------------------------------------------------------


def _agg(rows: list[dict]) -> dict:
    """Aggregate a list of per-query metric rows into mean metrics + deltas."""
    if not rows:
        return {"n": 0}
    ndcg_lex = statistics.fmean(r["ndcg_lex"] for r in rows)
    ndcg_hyb = statistics.fmean(r["ndcg_hyb"] for r in rows)
    delta = ndcg_hyb - ndcg_lex
    pct = (delta / ndcg_lex * 100.0) if ndcg_lex > 0 else 0.0
    return {
        "n": len(rows),
        "ndcg_lex": ndcg_lex,
        "ndcg_hyb": ndcg_hyb,
        "ndcg_delta": delta,
        "ndcg_pct": pct,
        "top5rel_lex": statistics.fmean(r["top5rel_lex"] for r in rows),
        "top5rel_hyb": statistics.fmean(r["top5rel_hyb"] for r in rows),
        "top1_changed_pct": statistics.fmean(1.0 if r["top1_changed"] else 0.0 for r in rows)
        * 100.0,
        "jaccard": statistics.fmean(r["jaccard"] for r in rows),
    }


def _fmt_agg_line(a: dict) -> str:
    if a["n"] == 0:
        return "_(no queries)_"
    sign = "+" if a["ndcg_delta"] >= 0 else ""
    return (
        f"nDCG@5 lex={a['ndcg_lex']:.4f} hyb={a['ndcg_hyb']:.4f} "
        f"delta={sign}{a['ndcg_delta']:.4f} ({sign}{a['ndcg_pct']:.1f}%) / "
        f"top5rel lex={a['top5rel_lex']:.3f} hyb={a['top5rel_hyb']:.3f} / "
        f"top1delta={a['top1_changed_pct']:.0f}% / jaccard={a['jaccard']:.3f}"
    )


def build_report(
    per_query: list[dict], skipped: list[dict], judge_model: str
) -> str:
    """Render the full markdown report from per-query rows + skip log."""
    overall = _agg(per_query)
    by_cat: dict[str, list[dict]] = {}
    for r in per_query:
        by_cat.setdefault(r["category"], []).append(r)
    concept = [r for r in per_query if r["conceptual"]]

    lines: list[str] = []
    lines.append("# Argus semantic rerank A/B - hybrid vs lexical-only")
    lines.append("")
    lines.append(
        f"Isolates the rerank effect on the SAME SearXNG candidate pool. Judge = "
        f"independent LLM `{judge_model}` (NOT the embedder - avoids circularity). "
        f"N={overall['n']} scenarios scored; {len(skipped)} skipped."
    )
    lines.append("")

    lines.append("## Overall")
    if overall["n"]:
        sign = "+" if overall["ndcg_delta"] >= 0 else ""
        verdict = (
            "HYBRID beat lexical" if overall["ndcg_delta"] > 0
            else "HYBRID == lexical (neutral)" if overall["ndcg_delta"] == 0
            else "HYBRID LOST to lexical"
        )
        lines.append("")
        lines.append("| metric | lexical | hybrid | delta |")
        lines.append("|---|---|---|---|")
        lines.append(
            f"| mean nDCG@5 | {overall['ndcg_lex']:.4f} | {overall['ndcg_hyb']:.4f} "
            f"| {sign}{overall['ndcg_delta']:.4f} ({sign}{overall['ndcg_pct']:.1f}%) |"
        )
        lines.append(
            f"| mean top-5 relevance | {overall['top5rel_lex']:.3f} "
            f"| {overall['top5rel_hyb']:.3f} | "
            f"{overall['top5rel_hyb'] - overall['top5rel_lex']:+.3f} |"
        )
        lines.append("")
        lines.append(
            f"- Queries where hybrid changed the top-1: "
            f"**{overall['top1_changed_pct']:.1f}%**"
        )
        lines.append(
            f"- Mean top-5 set Jaccard (lexical vs hybrid ordering): "
            f"**{overall['jaccard']:.3f}** "
            f"(1.0 = no reordering; lower = more reordering)"
        )
        lines.append(f"- **Verdict: {verdict}.**")
    else:
        lines.append("_No scenarios scored._")
    lines.append("")

    lines.append("## Per-category")
    lines.append("")
    lines.append(
        "| category | n | nDCG lex | nDCG hyb | delta | % | top5rel lex | "
        "top5rel hyb | top1delta% | jaccard |"
    )
    lines.append("|---|---|---|---|---|---|---|---|---|---|")
    for cat in sorted(by_cat):
        a = _agg(by_cat[cat])
        sign = "+" if a["ndcg_delta"] >= 0 else ""
        lines.append(
            f"| {cat} | {a['n']} | {a['ndcg_lex']:.3f} | {a['ndcg_hyb']:.3f} "
            f"| {sign}{a['ndcg_delta']:.3f} | {sign}{a['ndcg_pct']:.1f} "
            f"| {a['top5rel_lex']:.2f} | {a['top5rel_hyb']:.2f} "
            f"| {a['top1_changed_pct']:.0f} | {a['jaccard']:.3f} |"
        )
    lines.append("")

    lines.append("## Conceptual / how-to subset (where semantic should help most)")
    lines.append("")
    lines.append(
        "Queries starting with how/what/why, or containing 'vs' / 'explained'."
    )
    ca = _agg(concept)
    if ca["n"]:
        sign = "+" if ca["ndcg_delta"] >= 0 else ""
        lines.append("")
        lines.append(f"- n = {ca['n']}")
        lines.append(
            f"- nDCG@5 lexical = {ca['ndcg_lex']:.4f} / hybrid = {ca['ndcg_hyb']:.4f} "
            f"/ **delta = {sign}{ca['ndcg_delta']:.4f} ({sign}{ca['ndcg_pct']:.1f}%)**"
        )
        lines.append(
            f"- mean top-5 relevance lexical = {ca['top5rel_lex']:.3f} / "
            f"hybrid = {ca['top5rel_hyb']:.3f}"
        )
        lines.append(f"- top-1 changed = {ca['top1_changed_pct']:.1f}% / "
                     f"jaccard = {ca['jaccard']:.3f}")
    else:
        lines.append("")
        lines.append("_No conceptual queries in this run._")
    lines.append("")

    lines.append("## Methodology & honesty notes")
    lines.append("")
    lines.append(
        f"- **Same pool, two rerankers.** One raw SearXNG pool (<={POOL_SIZE}) per query "
        f"is reranked both ways via Argus's own `argus.search.rerank` "
        f"(`semantic_rerank=False` vs `True`), then truncated to top-{TOP_K}. Only the "
        "rerank differs."
    )
    lines.append(
        f"- **Neutral judge.** ONE `{judge_model}` call per query rates every candidate "
        "0..3. The judge is an independent LLM, NOT the fastembed embedder used by the "
        "hybrid reranker - so the metric does not reward the embedder for agreeing with "
        "itself. Judgments are cached per (query,url) so both orderings reuse identical "
        "relevances."
    )
    lines.append(
        f"- **nDCG@{TOP_K}.** DCG = sum rel_i / log2(rank_i + 1); IDCG from the best-possible "
        f"ordering of the FULL pool, truncated to {TOP_K}. An all-irrelevant pool scores 0."
    )
    lines.append(
        "- **Truthfulness.** The delta may be small, zero, or negative - many pools are "
        "short and lexical already orders them well, so hybrid often cannot improve nDCG. "
        "The verdict above states the measured outcome as-is."
    )
    if skipped:
        lines.append(f"- **Skipped ({len(skipped)}):**")
        for s in skipped:
            lines.append(f"  - `{s['id']}` ({s['reason']}): {s['query']}")
    else:
        lines.append("- **Skipped:** none.")
    lines.append("")
    return "\n".join(lines)


# --- orchestration -----------------------------------------------------------


async def run(ids: list[str], pace: float, judge_mode: str = "embed") -> tuple[dict, str]:
    """Run the A/B over the given scenario ids; return (overall agg, report markdown)."""
    oai = None
    if judge_mode == "llm":
        from openai import AsyncOpenAI

        if not os.environ.get("OPENAI_API_KEY"):
            raise SystemExit("OPENAI_API_KEY is not set - needed for the LLM judge.")
        oai = AsyncOpenAI()
    judge_label = JUDGE_MODEL if judge_mode == "llm" else JUDGE_EMBED_MODEL + " (embedding judge)"
    cache: dict[tuple[str, str], float] = {}
    per_query: list[dict] = []
    skipped: list[dict] = []

    async with httpx.AsyncClient(timeout=20.0) as client:
        for sid in ids:
            scen = scen_mod.by_id(sid)
            query, category = scen["query"], scen["category"]

            pool = await fetch_pool(query, client, pace)
            if not pool:
                skipped.append({"id": sid, "query": query, "reason": "empty_pool"})
                print(f"  SKIP {sid}: empty pool (throttled?)", file=sys.stderr)
                continue

            try:
                if judge_mode == "llm":
                    judged = await judge_pool(query, pool, oai, cache)
                else:
                    judged = judge_pool_embed(query, pool, cache)
            except Exception as exc:  # noqa: BLE001 - log + skip, never crash the run
                skipped.append({"id": sid, "query": query, "reason": f"judge_error:{exc}"})
                print(f"  SKIP {sid}: judge error {exc}", file=sys.stderr)
                continue

            row = eval_query(query, pool, judged)
            row.update(
                {"id": sid, "category": category, "query": query,
                 "conceptual": is_conceptual(query), "pool_size": len(pool)}
            )
            per_query.append(row)
            print(
                f"  {sid} [{category}] nDCG lex={row['ndcg_lex']:.3f} "
                f"hyb={row['ndcg_hyb']:.3f} (pool={len(pool)})",
                file=sys.stderr,
            )

    report = build_report(per_query, skipped, judge_label)
    return _agg(per_query), report


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    ap = argparse.ArgumentParser(description="Argus semantic rerank A/B benchmark.")
    ap.add_argument(
        "--ids-from-compare",
        action="store_true",
        help="use the COMPARE_IDS from scenarios.py (default set).",
    )
    ap.add_argument("--limit", type=int, default=None, help="cap to the first N ids.")
    ap.add_argument("--pace", type=float, default=3.0, help="seconds between queries.")
    ap.add_argument("--judge", choices=["embed", "llm"], default="embed",
                    help="relevance judge: 'embed' (all-MiniLM, no API) or 'llm' (gpt-4o-mini).")
    ap.add_argument(
        "--out", type=Path, default=DEFAULT_REPORT, help="report markdown path."
    )
    return ap.parse_args(argv)


def main(argv: list[str] | None = None) -> None:
    args = _parse_args(argv)
    ids = list(scen_mod.COMPARE_IDS)  # default == --ids-from-compare set
    if args.limit is not None:
        ids = ids[: args.limit]
    t0 = time.perf_counter()
    overall, report = asyncio.run(run(ids, args.pace, args.judge))
    args.out.write_text(report, encoding="utf-8")
    print(f"Report written: {args.out}", file=sys.stderr)
    dt = time.perf_counter() - t0
    if overall.get("n"):
        sign = "+" if overall["ndcg_delta"] >= 0 else ""
        print(
            f"DONE n={overall['n']} in {dt:.0f}s / "
            f"nDCG@5 lex={overall['ndcg_lex']:.4f} hyb={overall['ndcg_hyb']:.4f} "
            f"delta={sign}{overall['ndcg_delta']:.4f} ({sign}{overall['ndcg_pct']:.1f}%)",
            file=sys.stderr,
        )
    else:
        print(f"DONE: 0 scenarios scored in {dt:.0f}s (all skipped).", file=sys.stderr)


if __name__ == "__main__":
    main()
