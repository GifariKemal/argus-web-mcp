# Argus Benchmark Harness

Reference-based + reference-free benchmark for the Argus web fetch/scrape/search
MCP, scoring it against **free** scraper baselines and against the native web
tools of Claude Code and Codex CLI on the same queries.

For the durable, tracked results (numbers + findings), read
[RESULTS.md](RESULTS.md). This README documents the harnesses that produce them.

## Contents

- [Two benchmark families](#two-benchmark-families)
- [Extraction-quality harness (files)](#extraction-quality-harness-files)
- [Comparison harness (files)](#comparison-harness-files)
- [Running](#running)
- [Metrics](#metrics-stdlib-token-level-lowercased-whitespace-tokens)
- [Gold curation - read this](#gold-curation---read-this)
- [Adapters](#adapters)

## Two benchmark families

| Family | What it answers | Entry points | Results |
|---|---|---|---|
| Extraction quality | Does Argus capture main content and reject boilerplate vs free scrapers? | `run_bench.py`, `scorer.py`, `adapters.py` | `report.md` (regenerable) |
| Comparison | How does Argus stack up against Claude/Codex native web tools (breadth, depth, tokens, cost, speed)? | `run_compare.py`, `run_4way.py`, `scenarios.py`, `run_codex.sh`, `burst_test.py` | [RESULTS.md](RESULTS.md) |

## Extraction-quality harness (files)

| File | Role |
|---|---|
| `testset.yaml` | The active non-trading test set: 16 URL items + 8 search queries. Each item may carry a `gold_reference` pointing to a curated `gold/<id>.md`. |
| `quality_gold.yaml` | **The FAIR gold.** 2 hand-verified non-trading items spanning page types, each with `must_contain` (main-content phrases that must be captured) + `must_not_contain` (real boilerplate that must be absent). Formatting-agnostic - favours no adapter. |
| `scorer.py` | Stdlib-only metrics (no rouge/nltk dep): legacy `rouge_l`, `token_f1`, `truncation_completeness`; **fair `content_recall`, `boilerplate_rejection`, `quality_f1`**; plus `success`, `lcs_len`, `score_item`. Run `python benchmark/scorer.py` for an assert self-check. |
| `adapters.py` | Uniform `async run(item) -> {content, latency, ok}`. Adapters: `argus` (real in-process tool), `raw_trafilatura`, `readability_only` (free baselines). Paid adapters are a documented stub in `KEYED_ADAPTERS` (skipped - never called without a key). |
| `run_bench.py` | Runner - loads testset, runs adapters, scores, writes `report.md`. |
| `regold.py` | Regenerates the independent gold with a neutral third extractor (see [Gold curation](#gold-curation---read-this)). |
| `gold/<id>.md` | Curated clean main-text for the stable text items. |
| `report.md` | Generated leaderboard + per-item + "where Argus loses" + exit-gate. |

## Comparison harness (files)

| File | Role |
|---|---|
| `scenarios.py` | The query instrument: `SCENARIOS` = 160 real non-trading queries across 8 SURIOTA-relevant categories (~20 each); `COMPARE_IDS` = a 40-id stratified sample (exactly 5 per category) for the expensive head-to-head runs. Run `python benchmark/scenarios.py` for the self-check. |
| `run_compare.py` | 3-arm harness. `argus` runs `search()` over all 160 active scenarios (paced for SearXNG per-IP throttle); `argus-research` runs `research()` over the compare ids; `score` aggregates the sweep + auto-flags weak categories; `merge-3way` builds the Argus-vs-Claude-vs-Codex section. Aggregation is pure functions (offline-tested in `tests/test_compare_scorer.py`). |
| `run_4way.py` | 4-condition harness with `--repeat` plus token / cost / speed: claude-native, claude-argus, codex-native, codex-argus. Shells out to the CLIs (`run`), then `score` renders the table. Pure parse/aggregate functions are offline-tested in `tests/test_4way_scorer.py`. Per-call wall-clock cap 180s. |
| `run_codex.sh` | Drives Codex CLI (`web_search=live`) over the 40 `COMPARE_IDS`, one raw answer per scenario to `codex_compare/<id>.txt`. Idempotent (skips ids that already have non-empty output, so it resumes after a stop). |
| `burst_test.py` | Un-paced burst re-validation: exercises Argus backoff + multi-engine redundancy vs a naive raw client to measure throttle recovery. |
| `loadtest.py` | Local load test (no OOM / leak check). |
| `semantic_ab.py` | Semantic rerank A/B: scores the same SearXNG candidate pool reranked lexical vs hybrid at nDCG@5 (see RESULTS.md). |

## Running

### Extraction-quality harness

```bash
# default = curated subset (items that have a real gold/<id>.md), live network
./.venv/Scripts/python.exe benchmark/run_bench.py

# filter
./.venv/Scripts/python.exe benchmark/run_bench.py --categories docs,longform
./.venv/Scripts/python.exe benchmark/run_bench.py --ids longform-04,docs-03
./.venv/Scripts/python.exe benchmark/run_bench.py --limit 3

# offline: only items with checked-in gold; skips network baselines
./.venv/Scripts/python.exe benchmark/run_bench.py --offline

# FAIR extraction-quality gate (formatting-invariant quality_f1) - use THIS, not ROUGE-L
./.venv/Scripts/python.exe benchmark/run_bench.py --quality
```

The runner is resilient: a failing fetch for one item is caught and recorded
(`ok=False`), never crashing the run. Every skipped item is logged with a reason
(no silent caps).

### Comparison harness

```bash
# 3-arm: Argus search over all 160 active scenarios, then aggregate + auto-flag
./.venv/Scripts/python.exe benchmark/run_compare.py argus --out out.json --pace 4.0
./.venv/Scripts/python.exe benchmark/run_compare.py score --argus out.json

# Argus research() over the 40 compare ids, then merge the 3-way section
./.venv/Scripts/python.exe benchmark/run_compare.py argus-research --out r.json --ids-from-compare
./.venv/Scripts/python.exe benchmark/run_compare.py merge-3way --argus-research r.json \
    --claude claude.json --codex-dir benchmark/codex_compare

# 4-condition (Claude/Codex x WITH/WITHOUT Argus) with token/cost/speed
./.venv/Scripts/python.exe benchmark/run_4way.py run --out out.json --repeat 3
./.venv/Scripts/python.exe benchmark/run_4way.py score --in out.json

# Codex native answers over the 40 compare ids (idempotent, resumable)
bash benchmark/run_codex.sh
```

## Metrics (stdlib, token-level, lowercased whitespace tokens)

- **ROUGE-L** - LCS-based F1 between prediction and gold. `0.0` if either empty.
- **token-F1** - multiset (bag-of-tokens) overlap F1.
- **truncation completeness** - `min(1.0, |pred|/|gold|)`; measures long-form coverage.
- **success** - non-error dict with non-empty content.
- **latency** - wall time of the adapter call (`time.perf_counter()`).

Reference-free mode (no gold) reports only `success`, `latency`, and `pred_words`.

### Extraction-quality (formatting-invariant) - the FAIR metric / `--quality`

**ROUGE-L against a raw-text gold is CONFOUNDED**: it rewards the least-transformed
output (a raw DOM dump) and penalises Argus's clean Markdown (dropped nav/ads, fenced
code, restructured tables). `quality_f1` fixes this by measuring the thing we actually
want - "captured the main content AND left out the boilerplate" - independent of format:

- **content_recall** - fraction of `must_contain` gold phrases present in the output. A
  phrase is "present" if its word-tokens appear as a `>=0.8`-overlap run anywhere in the
  output. Both sides are word-tokenised (punctuation/markdown stripped), so `**OpenAPI**`,
  `"schema"` and bare `openapi`/`schema` match identically. `1.0` if `must_contain` empty.
- **boilerplate_rejection** - fraction of `must_not_contain` strings ABSENT (case-insensitive
  substring). A raw full-page dump that keeps nav/ads/footer scores LOW here. `1.0` if empty.
- **quality_f1** - harmonic mean of the two. A raw dump -> recall 1.0 but rejection low -> low f1;
  an over-trimmer that drops body text -> low recall -> low f1. Favours no adapter by construction.

Gold lives in `quality_gold.yaml` (2 active non-trading items: FastAPI docs and the
near-zero-boilerplate Paul Graham essay). `must_contain` phrases are
verbatim main-content; `must_not_contain` are real nav/subscribe/cookie/footer/ad strings
fetched live from each page. `run_bench.py --quality` runs every free adapter on those URLs,
writes an "Extraction-quality (formatting-invariant)" section to `report.md`, and marks the
legacy ROUGE-L sections as confounded. **This is the gate to read; ROUGE-L is kept only for
historical comparison.**

## Gold curation - read this

**P2 update (2026-06-24): gold is now INDEPENDENTLY curated.** The P1 gold was
extracted *with Argus itself* (trafilatura), giving Argus a meaningless home-advantage
ROUGE-L of 1.000. The active non-trading re-curated items - `longform-04`, `docs-01` -
were curated with a **neutral third extractor** via `benchmark/regold.py`:
BeautifulSoup `.get_text()` over each page's main-content DOM node (`<font>` for the
Paul Graham essay, `<article>` for the FastAPI doc),
with only universal furniture removed (`script`/`style`/`nav`/`header`/`footer`, plus
Wikipedia ref/edit markers). This is a plain DOM text-dump with **no article-detection
heuristic**, so it is independent of BOTH Argus (trafilatura) AND the readability
baseline (readability-lxml) and favours no adapter under test. Each gold file's first
line is an HTML comment recording the source URL, the neutral method, the date, and an
explicit "independent of Argus" note; `run_bench.py` strips that comment before scoring.

To regenerate the independent gold:

```bash
./.venv/Scripts/python.exe benchmark/regold.py
```

**Honest consequence.** Against this neutral gold Argus no longer wins on ROUGE-L -
the exit-gate "Argus ROUGE-L >= best free baseline" **FAILS** on the 3-item subset
(`readability_only` 0.955 > `raw_trafilatura` 0.891 > `argus` 0.849). This is a gold-style
artifact, not an extraction-quality regression: ROUGE-L against a raw text-dump rewards the
*least* transformed output, and `readability_only` is itself a tag-strip closest to that dump,
whereas Argus emits restructured Markdown (headings, code fences) that diverges from raw text.
See `report.md` -> "Honest gate read" for the full per-item analysis. A fair future gate should
score against a hand-normalised canonical reference or add a structure-aware metric rather than
penalising Markdown formatting.

The two remaining P1 gold files (`longform-03`, `docs-03`) were NOT re-curated in this pass and
are still Argus-extracted approximations - exclude them from honest-gate reads until re-golded.

Independently re-curated items: `longform-04` (Paul Graham essay), `docs-01` (FastAPI
tutorial).

## Adapters

- **argus** - calls `argus.server.read()` (or `read_pdf()` for `pdf` category)
  in-process against a real SSRF-guarded client + real Crawl4AI browser. The
  runner owns setup/teardown (`setup_argus()` / `teardown_argus()`).
- **raw_trafilatura** - `httpx.get` (static, no JS) + `trafilatura.extract`. A
  typical free static scraper; fails/empties on JS-heavy or bot-walled pages.
- **readability_only** - `httpx.get` + readability-lxml `Document.summary()` ->
  tag-stripped text. Another free baseline.
- **paid (Jina/Firecrawl/Exa/Tavily)** - `KEYED_ADAPTERS` stub, intentionally
  empty in P1. Not called (would cost money + need secrets).
