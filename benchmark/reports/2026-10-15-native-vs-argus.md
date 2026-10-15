# Argus vs Claude Code native WebSearch / WebFetch (2026-10-15)

Live measurement from the SURIOTA workstation against the deployed Argus (`https://argus.gifariksuryo.xyz/mcp`, 0.4.29) and the native web tools of Claude Code CLI 2.1.300. Everything below is measured, not quoted from docs, except the pricing and availability rows, which name their source.

> [!NOTE]
> Raw captures and the three harness scripts live in `benchmark/native/` (`native_capture.py`, `argus_direct.py`, `score_native.py`). Re-run order: capture native search + fetch, run Argus direct, then score.

## Method

Three layers, so that tool output, tool depth and the end-to-end agent experience are each measured on their own terms.

| Layer | What is compared | How |
|---|---|---|
| 1. Tool output | native `WebSearch` vs Argus `search`; native `WebFetch` vs Argus `read` | Native: a headless `claude -p --output-format stream-json` run on `claude-haiku-5-5` exposes every `tool_result` block verbatim, so the measurement is the tool's own output, not a model paraphrase. Latency = `tool_use` to `tool_result` timestamps inside Claude Code. Argus: direct MCP calls to the VPS from a `fastmcp` client, latency = client wall clock (includes Cloudflare + Traefik + VPS). `read` URLs carry a cache-busting query string, so every Argus read is cold. |
| 2. Depth | one Argus `research(deep, 5 sources)` call per query | Same 24 queries. Native search has no equivalent (it returns links and a summary, never page content). |
| 3. Agent level | `claude -p` with WebSearch + WebFetch only vs `claude -p` with the Argus MCP only | The repo's `benchmark/run_4way.py`, 24 scenarios, same prompt ("research this question, list sources, 3-5 sentence synthesis"), same model, CLI-reported cost and tokens. `--setting-sources ""` so the fixed overhead is the CLI's own system prompt, not the operator's CLAUDE.md. |

Query set: 24 of the repo's 40 stratified compare scenarios (3 per category: dev, firmware, web, ai_ml, news, docs, science, business). URL set: the 16 items of `benchmark/testset.yaml` (docs, long-form, SPA, PDF), 4 of them with curated gold main-text.

## 1. Search: native WebSearch vs Argus `search` (24 queries)

| metric | native WebSearch | Argus search |
|---|---|---|
| queries answered (no error, >0 results) | 24/24 | 24/24 |
| results per query (mean) | 9.25 | 10 |
| unique domains per query (mean) | 7.54 | 9.04 |
| per-result snippet | none: a links list plus ONE model-written summary of the results | 9.96 of 10 hits |
| payload chars per query (mean) | 3251 | 2815 |
| top-3 title/query overlap (relevance proxy) | 0.48 | 0.71 |
| tool latency p50 / p95 (s) | 6.42 / 7.85 | 3.80 / 5.78 |
| engines answering (mean) | 1 (undisclosed) | 2.46 (named per hit) |
| shared domains between the two result lists (mean) | 1.96 of ~8 | |
| result dates | none in the tool output | none (SearXNG JSON carries no date for these engines) |
| served from cache | not visible | 0/24 (field `from_cache`) |
| tool fee per query | $0.01 (web search is $10 per 1,000 searches, Claude Platform docs "Web search tool", plus result tokens) | $0 (self-hosted; VPS is a fixed cost) |

Per category (results / unique domains / top-3 overlap):

| category | native | Argus |
|---|---|---|
| ai_ml | 9.33 / 5.00 / 0.37 | 10 / 9.67 / 0.67 |
| business | 9.67 / 9.67 / 0.39 | 10 / 9.67 / 0.63 |
| dev | 9.00 / 8.67 / 0.57 | 10 / 9.33 / 0.84 |
| docs | 9.33 / 8.33 / 0.49 | 10 / 8.67 / 0.71 |
| firmware | 9.00 / 5.67 / 0.47 | 10 / 8.00 / 0.52 |
| news | 9.00 / 9.00 / 0.54 | 10 / 8.67 / 0.78 |
| science | 9.67 / 6.33 / 0.58 | 10 / 9.33 / 0.85 |
| web | 9.00 / 7.67 / 0.46 | 10 / 9.00 / 0.65 |

What the two outputs actually are. The native tool returns `Links: [{title,url}...]` followed by a paragraph the search backend's model wrote about the results, ending in "REMINDER: You MUST include the sources above in your response". The agent never sees a snippet per result, so it cannot judge or re-rank individual hits; it can only trust the summary or fetch pages. Argus returns ranked hits with title, URL, snippet and the engine that answered, merged across 2-3 engines with semantic rerank, plus `degraded`/`engines_used` so the agent knows how good the answer is. The relevance proxy (query words in the top-3 titles) is 0.71 vs 0.48; the two lists share only ~2 domains per query, so they are not the same index.

## 2. Depth: Argus `research(deep)` (24 queries)

| metric | Argus research(deep, 5 sources) |
|---|---|
| queries with >=1 full source | 24/24 |
| sources per query (mean) | 5.0 (backfilled when a candidate fails) |
| full-content words per query (mean / median) | 8,410 / 8,348 |
| latency p50 / p95 (s) | 7.2 / 34.4 |
| candidate fetches that failed (total, backfilled) | 17 over 24 queries |

One call returns ~8.4k words of extracted page content from five sources. The native path needs one WebSearch plus five WebFetch calls to approximate this, and WebFetch does not return the content (section 3).

## 3. Read: native WebFetch vs Argus `read` (16 URLs)

| metric | native WebFetch | Argus read |
|---|---|---|
| URLs that returned page content | 13/16 (3 PDFs returned a refusal: the tool handed the model raw PDF bytes) | 16/16 |
| words returned per URL (mean / median) | 551 / 290 | 26,476 / 12,722 |
| asked for the full page verbatim: did it comply | 3/16 (two FastAPI pages, one MDN page); 13/16 answered "I can't reproduce the full page verbatim, here is a concise summary" | 16/16 |
| page window | 100,000 chars per call, then "call again with offset" (5/16 pages hit it) | no cap (streaming body cap only) |
| tool latency p50 / p95 (s) | 7.22 / 34.67 | 2.04 / 16.98 (cold; 2 of 16 needed the browser tier) |
| structured metadata | none | title, site, lang, date, author (3.1 of 5 populated on average) |
| PDF | not supported (binary passed to the model) | extracted (7k, 21k and 271k words) |
| JS-rendered page (react.dev) | summary only (643 words) | 2,769 words, static tier |
| how the text is produced | page -> markdown -> a small model answers the prompt against it (non-deterministic, non-verbatim) | deterministic extraction, no model |
| caching | 15 minutes per URL, not visible to the caller | per-source TTL, `from_cache` and `stale` flagged |

Against the curated gold main-text (token F1):

| item | gold words | native words | native F1 | Argus words | Argus F1 |
|---|---|---|---|---|---|
| docs-01 (FastAPI first steps) | 1,799 | 324 | 0.166 | 1,761 | 0.791 |
| docs-03 (FastAPI index) | 2,116 | 236 | 0.096 | 2,009 | 0.944 |
| longform-03 (Wikipedia, monetary policy) | 16,222 | 327 | 0.032 | 16,342 | 0.919 |
| longform-04 (Paul Graham essay) | 11,982 | 255 | 0.031 | 11,794 | 0.983 |
| **mean** | | | **0.08** | | **0.91** |

Read this fairly: WebFetch is built to answer a question about a page with a small model, and for that it works. It is not an extractor. When the job is "give me the page" (docs to quote, a spec to follow, an article to cite), it returns a 250-350 word summary with a disclaimer, and for long pages only the first 100k characters were even seen. Argus `read` returns the page.

## 4. Agent level: `claude -p` end to end (24 scenarios)

Condition `claude-native` = WebSearch + WebFetch only. Condition `claude-argus` = the Argus MCP (`research`, `search`, `read`) only. Same prompt, same model (the CLI default, Claude Fable 5.1), `--setting-sources ""`, one repetition, 48 runs, 0 errors, 0 timeouts.

| metric | claude-native | claude-argus | delta |
|---|---|---|---|
| answered (found a source + synthesis) | 24/24 | 24/24 | |
| cost per query, CLI-reported `total_cost_usd` (mean / median) | $0.211 / $0.171 | $0.136 / $0.128 | **-36% / -25%** |
| total for the 24 queries | $5.06 | $3.27 | -$1.79 |
| wall latency p50 / p95 (s) | 28.4 / 37.3 | 39.9 / 76.2 | +40% / +104% |
| sources cited in the answer (mean) | 6.67 | 5.08 | |
| synthesis length (words, mean) | 290 | 289 | same |

Per category (latency p50 native/Argus, cost mean native/Argus):

| category | latency p50 | cost |
|---|---|---|
| ai_ml | 25 s / 36 s | $0.20 / $0.14 |
| business | 29 s / 35 s | $0.21 / $0.16 |
| dev | 28 s / 37 s | $0.27 / $0.15 |
| docs | 20 s / 32 s | $0.16 / $0.12 |
| firmware | 28 s / 48 s | $0.25 / $0.14 |
| news | 29 s / 40 s | $0.27 / $0.14 |
| science | 25 s / 44 s | $0.16 / $0.11 |
| web | 29 s / 36 s | $0.16 / $0.15 |

Reading it: with Argus the agent makes one `research` call and writes; with the native tools it searches, then fetches several pages, each fetch a model round trip, which is why the native run costs more tokens even though its answer is the same length. The native run is faster end to end because WebSearch's summary is already "pre-digested" and the agent writes from it, while Argus hands back ~8k words that the agent reads before writing. The cost figure is tokens only (CLI `total_cost_usd`); it does not include the $10 per 1,000 web search fee, which would widen the gap. `run_4way`'s `total_tokens` counts uncached input + output only and is not comparable across the two arms (the native result text is cached by Claude Code), so cost is the honest token proxy here.

## 4b. Same measurement after deploying 0.4.29-0.4.31 (same day, same harness, cold cache)

The sections above were measured against 0.4.27. After the extraction pool, the
single-pass extractor and the first-done-wins bundle went live (plus 0.4.31's 30 s
extraction ceiling), `argus_direct.py` was run again from the same workstation:

| metric | 0.4.27 | 0.4.31 |
|---|---|---|
| search p50 / p95 / max (s) | 3.8 / 5.8 / 8.7 | 3.1 / 3.7 / 3.8 |
| research(deep) p50 / p95 / max (s) | 7.2 / 34.4 / 36.4 | 7.1 / 9.8 / 9.9 |
| research sources per query, failed candidates | 5.0, 17 | 5.0, 4 |
| research words per query (mean) | 8,410 | 9,125 |
| read p50 / p95 / max (s), cold | 2.5 / 17.0 / 17.0 | 1.3 / 6.6 / 6.6 |
| read URLs with content, browser-tier reads | 16/16, 2 | 16/16, 0 |

Same yield, same or more content, and the tails that the first run flagged are gone: the
slowest research call is now under 10 s against 36 s, the slowest read 6.6 s against 17 s.

The agent-level `claude-argus` arm was then re-run (same 24 scenarios, prompt and model;
the native arm is the morning's run):

| arm | answered | cost mean / median | latency p50 / p95 / max (s) | sources cited | words |
|---|---|---|---|---|---|
| claude-native (0.4.27 run) | 24/24 | $0.211 / $0.171 | 28.4 / 37.3 / 41.8 | 6.67 | 290 |
| claude-argus on 0.4.27 | 24/24 | $0.136 / $0.128 | 39.9 / 76.2 / 96.0 | 5.08 | 289 |
| claude-argus on 0.4.31 | 24/24 | $0.122 / $0.104 | **20.3 / 36.8 / 43.7** | 5.12 | 274 |

With the research tail fixed, the Argus-equipped agent is now faster than the native one
at p50 (20 s vs 28 s), equal at p95, and 42% cheaper per query; every one of the 24
scenarios got faster (worst case 96 s -> 35 s).

## 4c. Answer quality: a blind judge, and what it changed

Both arms were re-run with the answer text kept (second repetition: native $0.214, p50
27.3 s; Argus $0.121, p50 22.1 s; 24/24 each), then Claude Sonnet 5.5 graded each pair
blind (A/B randomised; the whole pass repeated with A/B swapped).

| | native arm | Argus arm |
|---|---|---|
| wins, consistent in both orders (24 pairs) | 15 | 4 (5 flipped with position) |
| accuracy (1-5, pooled) | 4.21 | 4.06 |
| source quality (1-5) | 3.94 | 3.27 |
| specificity (1-5) | 4.50 | 3.90 |
| authoritative-looking citations | 24% (arxiv 8x, docs.espressif 4x) | 12% (zeonedge, imraan.in, dataintelo) |

The pool was not the problem: a 30-hit probe found authoritative pages at 19-20% of ranks
11-30 but 15% of the top 10. The rerank preferred keyword-dense SEO pages. 0.4.33 adds a
bounded authority prior; offline on these pools it lifts the authoritative share of the
top 5 from 14% to 48% (native's list: 32%) with 4.75 of the old top 5 still in the top 10.
The judge is re-run after deploy to confirm the answers improve, not just the ranks.

## 4d. Multi-user load (live VPS)

| concurrent agents (research deep + read each) | research p50 / max | read p50 / max | errors |
|---|---|---|---|
| 1 | 5.4 / 5.4 s | 2.0 / 2.0 s | 0 |
| 3 | 12.9 / 16.8 s | 0.6 / 1.8 s | 0 |
| 5 | 15.2 / 20.4 s | 1.3 / 1.9 s | 0 |
| 8 | 26.2 / 45.9 s | 1.8 / 3.2 s | 0 |

Reads stay fast; research scales with the two extraction workers on two vCPUs.

## 4e. Indonesian queries (16, same harness)

| metric | native WebSearch | Argus search |
|---|---|---|
| Indonesian-language hits (title/snippet proxy) | 63 of 147 | 106 of 160 |
| `.id` domains | 31 | 35 |
| tool latency p50 | 8.0 s | 4.0 s |

Argus research: 5 sources, 5,960 words, p95 18.4 s. Reads: 4 of 6 Indonesian pages in
full; `kemenperin.go.id` refuses the VPS (fine from a residential IP; a WARP candidate),
`idx.co.id` is a Cloudflare wall from everywhere. WebFetch summarised all six.

## 5. Factors that are not numbers

| factor | native WebSearch / WebFetch | Argus |
|---|---|---|
| what the agent receives | links + a backend-written summary; page answers by a small model | ranked hits with snippets and engines; full extracted content; PDFs; links/images; structured metadata |
| controllability | `allowed_domains` / `blocked_domains` on search; a prompt on fetch | include/exclude domains, `category` (general/news/science/it), `time_range`, `lang`, `safesearch`, `count`, `format`, `clean`, `include_links`, `extract_media`, selectors and LLM extraction, `max_sources`, `highlights`, `timeout` |
| tool surface | 2 tools | 20 tools: search, smart_search, research, read, read_pdf, scrape (JS, clicks), screenshot, batch_read, crawl, map_urls, extract_structured, find_similar, github_search, scholar_search, watch/list/unwatch, forexfactory_calendar, cot_report, news_sentiment_feed |
| anti-bot / blocked sites | opaque: a blocked page comes back as whatever the small model says | static -> browser -> stealth -> Wayback ladder; Cloudflare WARP egress for hosts that block the VPS IP; honest `blocked_by_antibot` / `ssrf_blocked` / `dns_failed` codes with `retryable` |
| transparency | which engine, how many results considered, whether cached: not exposed | `engines_used`, `degraded` + reason, `from_cache`, `stale` + `cached_at`, `render_path`, `/metrics` per tool and per pipeline stage |
| availability | WebSearch is US-only (tool description); both depend on Anthropic's service | depends on the SURIOTA VPS and Cloudflare; measured daily by `reachability.py` and `uptime.yml` |
| privacy / ownership | queries and pages pass through Anthropic's search backend and its model | queries hit the SURIOTA SearXNG and the sites directly; nothing leaves the owned infrastructure except the fetches themselves |
| cost model | $10 per 1,000 searches plus the tokens of every result; WebFetch billed as tokens of the processed page | VPS fixed cost (Tencent 2 vCPU / 7 GB, shared with other stacks); no per-call fee; search engines are free tiers with per-IP throttles |
| rate limits | Anthropic per-org web search limit (Console); fetch cache 15 min | per-host courtesy throttle + circuit breaker; SearXNG engine cooldowns; a limited set of engines answer from this IP (bing, brave, google, google cse, duckduckgo web, yandex) |
| failure modes seen in this run | PDFs unreadable; verbatim requests refused; 100k-char window; no per-result snippets | 17 of 120 research candidate fetches failed and were backfilled; browser-tier reads cost 12-17 s; research p95 34 s |
| maintenance | none | yours: engine list drift (measured per release), WARP account, Chromium image, VPS patching |

## Caveats

- n=24 queries and 16 URLs, one repetition. Enough to separate 0.08 from 0.91, not to resolve a 5% latency difference.
- Native tool latency was measured through the Claude Code CLI (tool_use to tool_result), Argus latency from a client on the same workstation through Cloudflare to the VPS. Both include network; neither includes model thinking time.
- Native search and fetch were driven by `claude-haiku-5-5` so that the captured tool output is cheap to obtain; the tool output itself does not depend on the outer model.
- The gold F1 for WebFetch measures "return the page", which the tool declines by design. For "answer a question about the page" it is usable and was not scored here.
- Argus results came from the production VPS during normal operation; nothing was pre-warmed, and reads were cache-busted.
