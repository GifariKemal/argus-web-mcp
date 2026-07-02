# Argus benchmark RESULTS (durable summary)

_Run 2026-06-24/25. Raw data (`argus_200.json`, `argus_research_*.json`, `claude_*.json`, `codex_25/`, `compare-report.md`) is gitignored/regenerable; this file is the tracked historical record._

Historical 2026-06-24/25 runs used 200 queries x 10 categories and 50 compare IDs. Current v0.4.4 harness scope is 160 non-trading queries x 8 categories and 40 compare IDs, with Codex outputs under `benchmark/codex_compare/`. See [README.md](README.md) for current harness usage.

## Contents

- [Argus search - 200 scenarios](#argus-search---200-scenarios-paced-4s)
- [3-way: Argus vs Claude WebSearch vs Codex CLI](#3-way-argus-vs-claude-websearch-vs-codex-cli-n25-then-n50)
- [Burst re-validation](#burst-re-validation-un-paced-15-queries)
- [Findings -> actions taken](#findings---actions-taken)
- [Competitor feature gap -> adopted](#competitor-feature-gap---adopted-see-docs05-competitive-gapmd)
- [Semantic rerank A/B](#semantic-rerank-ab---quantified-gain-2026-06-24)
- [4-way: Claude/Codex x WITH vs WITHOUT Argus](#4-way-claudecodex-x-with-vs-without-argus-n25-stratified-2026-06-25)
- [Controlled re-measure (post-fix)](#controlled-re-measure-2026-06-25-post-fix---supersedes-finding-3-above)

## Argus search - 200 scenarios (paced 4s)

- **100% success / 0% throttle / 0 no-results** across all 200 / 10 categories.
- Latency p50 1.42s / p95 2.51s / mean 9.66 results / relevance proxy (top-1 title overlap) 0.70.
- Engine answer distribution: **duckduckgo 189/200**, bing 55, brave 16, mojeek 7.
- No category breached the auto-flag thresholds (success<80 / overlap<0.3 / throttle>30).

## 3-way: Argus vs Claude WebSearch vs Codex CLI (n=25, then n=50)

Identical queries through each system's native path. Both N agree:

| arm | found | mean breadth | depth (content) |
|---|---|---|---|
| **Argus** `research` | 50/50 | 4.94 sources | **7,321 words FULL content/query** |
| Claude WebSearch | 50/50 | 9.1 hits | titles + URLs only |
| Codex CLI (`web_search` live) | 50/50 | 8.72 URLs | synthesized answer + citations (no raw content) |

- **Discovery parity** - all three find the answer on every scenario.
- **Depth = Argus's decisive edge** - one call returns ~5 sources of full extracted markdown (~7.3k words); competitors return hits / a summary. Argus research at n=50: **all 50 returned sources, 0 failures**.

## Burst re-validation (un-paced, 15 queries)

Engines were healthy this run: both Argus-resilient (backoff + multi-engine) and a naive raw client hit **100% success / 0% throttle** -> no throttle to recover from, so **no measurable delta today**. Redundancy was observably active (Argus pulled bing+ddg; naive only bing). The earlier ~5-10-query burst-throttle was not reproducible under current engine health. Honest read: backoff/redundancy are wired and active but not load-bearing on a healthy run; the durable fix for datacenter-IP throttle remains the deploy-time proxy pool.

## Findings -> actions taken

1. **Engine concentration risk** (ddg 189/200). -> multi-engine redundancy (default engine set) + auto-backoff retry on transient throttle (committed); proxy pool wired for deploy.
2. **Relevance-proxy artifact** on how-to/conceptual queries (title-only overlap). -> rerank v2 keeps snippet contribution; metric noted as a proxy artifact (3-way shows those queries still find good sources).
3. **Breadth vs depth** - research capped at 5 sources. -> `max_sources` exposed + `mode` (quick/deep/answer).
4. **No new Argus bugs** across 200 + 50 + 50 runs (0 errors). Harness build caught 1 real wiring bug (SSRF client on the trusted loopback search backend - fixed).

## Competitor feature gap -> adopted (see docs/05-COMPETITIVE-GAP.md)

Researched Jina / Brave / Firecrawl / Exa / Tavily / Bright Data. Adopted the top self-hostable gaps:

- **`map_urls`** - sitemap.xml / robots.txt / 1-hop link URL discovery (Firecrawl/Exa `map`).
- **`research(mode='answer')`** - cited LLM answer over the bundle (Exa/Tavily/Jina `answer`).
- **`find_similar`** - local embedding semantic similarity (Exa-style related pages).
- **search `include_domains`/`exclude_domains` + `safesearch` + recency-v2** (Exa/Tavily/Brave filters).
- Deferred (effort/infra): image captioning (VLM), managed residential proxies (Bright Data - only genuine non-self-hostable gap).

**Argus already matches/beats the field on:** full content (no truncation) vs lossy summaries/hits, unlimited+owned vs metered, self-hosted JS+stealth render, transparent archive egress-fallback, content-addressed persistent cache, and the trading-extractor moat.

## Semantic rerank A/B - quantified gain (2026-06-24)

Same SearXNG candidate pool reranked two ways via `argus.search.rerank` (lexical vs hybrid),
scored nDCG@5. Judge = an INDEPENDENT embedding model (all-MiniLM, different family from the
reranker's bge-small) - the neutral gpt-4o-mini judge was blocked by OpenAI quota (see finding).

| metric | lexical | hybrid | delta |
|---|---|---|---|
| mean nDCG@5 (n=50) | 0.7373 | 0.8428 | **+0.1055 (+14.3%)** |
| **conceptual/how-to subset (n=19)** | 0.6074 | 0.7729 | **+0.1655 (+27.3%)** |
| mean top-5 relevance | 0.941 | 0.983 | +0.042 |

- Hybrid changed the top-1 result on **50%** of queries; mean top-5 Jaccard 0.599 (substantial reordering).
- Gain is largest on conceptual/how-to queries (+27%) - the exact weak spot the lexical relevance proxy flagged in the 200-run. Confirmed.
- **Caveat:** the embedding judge leans semantic, so treat the magnitude as an upper-ish bound; a neutral LLM judge would tighten it. Direction (hybrid > lexical, biggest on conceptual) is robust.

### Finding: OpenAI key has no quota (429 insufficient_quota)

`OPENAI_API_KEY` is set but the account is out of credit. Impact: Argus's LLM-dependent features
(`research(mode='answer')`, `extract_structured` llm/auto) will FAIL at runtime with this key even
though `llm_available()` returns True (it checks key presence, not quota). FIX before relying on
LLM features: top up OpenAI, OR point `ARGUS_LLM_BASE_URL`/`ARGUS_LLM_API_KEY` at a self-hosted
(VPS Kimi) / Groq OpenAI-compatible endpoint (the provider-agnostic path already supports this).

## 4-way: Claude/Codex x WITH vs WITHOUT Argus (n=25 stratified, 2026-06-25)

Harness `benchmark/run_4way.py` (token+cost+speed). One web-research prompt per scenario, 4 conditions. Claude cost = CLI-reported ACTUAL; Codex cost = ESTIMATE (subscription auth reports only total tokens; blended gpt-5.5 $10/1M, ~20% output, marked `*`). Per-call hard-cap 180s.

| condition | success% | mean_tok | median_tok | cost/query | lat p50 | lat p95 | urls | words |
|---|---|---|---|---|---|---|---|---|
| claude-native | 100 | 26.9k | 31.8k | $0.528 | 30.5s | 46.8s | 3.6 | 177 |
| claude-argus | 92 | 26.0k | 33.0k | $0.570 | 46.3s | 169.5s | 3.3 | 195 |
| codex-native | 100 | 26.1k | 26.7k | $0.261* | 28.3s | 51.0s | 4.16 | 144 |
| codex-argus | 100 | 44.9k | 42.8k | $0.449* | 53.7s | 88.5s | 3.76 | 134 |

**WITH vs WITHOUT Argus:** Claude tokens -3.3% (median slightly up), cost +8%, synthesis +17 words (deeper), latency ~2x, 2/25 timeouts. Codex tokens **+72%**, cost +72%*, words -9, latency ~1.7x.

**Findings:**

1. **Claude leverages Argus efficiently** - token-neutral while producing the deepest synthesis (one `research` call returns full clean extracted content vs many native WebSearch hits). The right consumer for Argus.
2. **Codex does NOT** - +72% tokens, no quality gain; it makes many MCP round-trips that balloon context.
3. **Latency tail is the real cost** - the `*-argus` path adds latency and produced 2 claude-argus timeouts (p95 169s) because the single-uvicorn-worker deployed server + courtesy throttle serialize an agent's many tool calls. Normal single-user Argus latency is far lower (search ~1.4s, see above); these numbers are under one agent's burst. **Action (later SUPERSEDED - see [Controlled re-measure](#controlled-re-measure-2026-06-25-post-fix---supersedes-finding-3-above)): scale Argus to a worker pool / raise per-host concurrency for multi-call agent sessions.**
4. **Cost:** Argus adds ~8% $/query for Claude (full-content input vs hit lists); for Codex the estimate is +72% (token-driven).

## Controlled re-measure (2026-06-25, post-fix) - SUPERSEDES finding #3 above

A post-fix CLI re-run (v2) showed NO latency improvement and the native (no-Argus) control slowed too -> the v1->v2 difference was environmental noise, not the fixes. Two clean isolations explain it:

1. **Historical note, superseded by 0.3.0:** this run observed `research()` bypassing the host throttle. Current code threads `HostThrottle` through `server.research` -> `_research` -> `_deep_bundle` -> `_read_one`, so this is no longer an active gap.
2. **In-process `research()` on the VPS is FAST** (local SearXNG, no agent, no client transport): MQL5 query median **5.5s** (3.5-11.1s; first rep includes a one-time embed-model download), MQTT query median **3.6s** (3.3-5.3s). All returned 5 sources (backfill works: 1-2 per-query failures still yield 5).

**Conclusion:** Argus is NOT the latency bottleneck. The 50-180s `claude-argus` benchmark latencies are agent overhead (`claude -p` system prompt + reasoning + multi-turn) + MCP-over-HTTPS round-trips from the client + the agent making research()+many read() calls serially. There is **no Argus scaling fix to make** - the earlier "worker pool" action item is moot (and `--workers>1` would still be unsafe with the stateful pool). If end-to-end agent latency matters, the lever is the CONSUMING agent's pattern (one `research` call + synthesize, not research + many reads), not Argus. S2 (wiring ARGUS_MAX_CONCURRENT_CONTEXTS) remains a valid correctness fix for the dead doc'd knob, just not load-bearing here.

**Caveat:** claude-argus mean_tok is dragged by the 2 timeout records (partial, 0 tokens parsed); median (33.0k) is the robust central value. Codex $ is an estimate (no per-call billing exposed).
