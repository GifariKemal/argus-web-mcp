# Argus - Competitive Feature Gap Analysis (vs paid web tooling)

> _Created 2026-06-24._ Maps the web read/search/scrape feature sets of the leading **paid** products against Argus's tool surface at the time of analysis, then ranks what to adopt - favoring **self-hostable (OSS, no paid API)** options. All competitor features verified against official docs (URLs cited per section). Items we could not verify are flagged `[UNVERIFIED]`.

> [!NOTE]
> Status note (2026-06-25): this is a point-in-time analysis record. Several gaps flagged "P3-only / not built" below were subsequently **shipped** - `map` (`map_urls`), `watch` / `list_watches` / `unwatch`, `find_similar`, `research(answer)`, and domain filters are now live in the 20-tool set. The tables are preserved as the original gap-analysis snapshot; see `CHANGELOG.md` and `docs/03-TOOL-SPECS.md` for what shipped.

## Contents

- [Argus baseline (the 12, for reference)](#argus-baseline-the-12-for-reference)
- [1. Per-product feature tables](#1-per-product-feature-tables)
- [2. Missing / partial in Argus - ranked adoption list](#2-missing--partial-in-argus---ranked-adoption-list)
- [3. What Argus already matches or beats](#3-what-argus-already-matches-or-beats)
- [Top 5 self-hostable gaps to adopt (ranked)](#top-5-self-hostable-gaps-to-adopt-ranked)

## Argus baseline (the 12, for reference)

`read` (full markdown, no truncation, trafilatura+browser) / `search` (SearXNG multi-engine + rerank + backoff) / `read_pdf` (pymupdf4llm+Docling) / `scrape` (JS render + Patchright stealth + screenshot) / `batch_read` / `extract_structured` (CSS/XPath selector + LLM tier) / `crawl` (Crawl4AI BFS deep-crawl + robots) / `screenshot` / `research` (search->full-read top-K bundle, quick/deep) / trading extractors (`forexfactory_calendar`, `cot_report`, `news_sentiment_feed`) / + egress archive fallback + content-addressed cache.

---

## 1. Per-product feature tables

Legend: [x] = Argus has it / [ ] = missing / [~] = partial. Docs cited at end of each table.

### 1.1 Jina AI - Reader (`r.jina.ai`) / Search (`s.jina.ai`) / DeepSearch

<details>
<summary>Feature table</summary>

| Feature | Params / option | Argus? | Notes |
|---|---|:--:|---|
| URL -> clean markdown | (default Reader behavior) | [x] | `read` core, plus we do *no-truncation* full content |
| Image alt-text / captioning | `X-With-Generated-Alt` | [ ] | Generates captions for images lacking alt. Self-hostable via local VLM (BLIP/Florence-2) |
| Output format switch | `X-Respond-With` = markdown/text/html/screenshot/pageshot | [~] | `read.format` covers md/text/html; screenshot is in `scrape`/`screenshot`, not a Reader flag |
| Target CSS selector | `X-Target-Selector` | [x] | `extract_structured` (selector) / `scrape.wait_for`; not exposed on `read` |
| Wait-for selector | `X-Wait-For-Selector` | [x] | `scrape.wait_for` |
| Exclude elements | `X-Exclude` (remove selectors) | [~] | trafilatura strips boilerplate; no explicit per-call exclude-selector knob |
| JSON / schema extract | `X-Json-Schema`, `X-Instruction` | [x] | `extract_structured` (selector + LLM) |
| Links summary | `X-With-Links-Summary` -> "Buttons & Links" list | [~] | `read.include_links` keeps links inline; no deduped link-summary list |
| Images summary | `X-With-Images-Summary` -> image overview list | [ ] | No image-URL list output |
| Cache bypass / tolerance | `X-No-Cache`, `X-Cache-Tolerance` | [~] | Have content-addressed cache; need explicit per-call bypass + max-age flags |
| Proxy / geo | `X-Proxy`, `X-Proxy-Country` ('auto') | [~] | Egress/archive fallback exists; no per-call country-proxy routing |
| Locale | `X-Locale` (browser locale) | [ ] | No locale control on render |
| UA / referer override | `X-User-Agent`, `X-Referer` | [~] | Stealth sets UA; not user-overridable per call |
| Remove images | `X-Remove-Images` | [~] | implied by markdown clean; no explicit flag |
| Stream large pages | Stream Mode | [ ] | We return whole bundle (<=500k cap) |
| **Search** -> LLM-friendly full text | `s.jina.ai` returns top results *with full content* | [~] | Our `research` quick/deep does search->full-read bundle; `search` alone returns snippets only |
| **DeepSearch** agentic search-read-reason loop | OpenAI-compat chat API, citations, `team_size`, token budget, `boost_hostnames`/`bad_hostnames`/`only_hostnames`, JSON schema, `search_provider:arxiv` | [~] | `research` deep mode is the closest; not iterative/agentic, no answer synthesis, no hostname boost/budget controls |

Docs: https://jina.ai/reader/ / https://jina.ai/deepsearch/

</details>

### 1.2 Brave Search API

<details>
<summary>Feature table</summary>

| Feature | Params | Argus? | Notes |
|---|---|:--:|---|
| Web search | `q`, `count` (<=20), `offset` | [x] | SearXNG covers this, unlimited |
| Country / language | `country`, `search_lang`, `ui_lang` | [~] | `search.lang` only; no country/UI-lang split |
| Safe search | `safesearch` off/moderate/strict | [~] | SearXNG has safesearch; not exposed as Argus param |
| Freshness / time | `freshness` pd/pw/pm/py + custom `YYYY-MM-DDtoYYYY-MM-DD` | [~] | `search.time_range` day/week/month/year; **no custom date range** |
| Custom re-ranking | `goggles_id` / `goggles` | [ ] | Goggles = user-defined ranking lenses. Argus has rerank but no declarative ranking rules |
| Extra snippets | `extra_snippets` (<=5 excerpts/result) | [ ] | Single snippet per result |
| Result-type filter | `result_filter` | [~] | `search.category`; coarser |
| Rich result types | web, news, videos, locations, discussions, FAQ, infobox | [~] | SearXNG returns general/news/etc; no infobox/FAQ/locations structured blocks |
| Spellcheck | `spellcheck` | [ ] | none |
| Summary | `summary` (AI summary key) | [~] | covered by future `answer` mode, see gaps |

Docs: https://api-dashboard.search.brave.com/app/documentation/web-search/query

</details>

### 1.3 Firecrawl - scrape / crawl / map / extract / search

<details>
<summary>Feature table</summary>

| Feature | Params | Argus? | Notes |
|---|---|:--:|---|
| Scrape -> markdown/html/rawHtml | `formats` | [x] | `read`/`scrape` |
| Main-content only | `onlyMainContent` | [x] | `read.clean` (trafilatura) |
| Include/exclude tags | `includeTags`, `excludeTags` | [~] | selector extract only; no scrape-level tag filter |
| Wait / timeout | `waitFor`, `timeout` | [x] | `scrape.wait_for`, `timeout` |
| Browser actions | `actions`: wait, click, write, press, scroll, screenshot, executeJavascript, scrape | [~] | `scrape.actions` has click/scroll; **missing write/press/executeJavascript** |
| Screenshot | `screenshot`, `@fullPage` | [x] | `screenshot(full_page)` |
| Structured extract | `json` format / `extract` (schema or prompt) | [x] | `extract_structured` |
| Links list | `links` format | [~] | `include_links` inline; no clean link array |
| Images list | `images` format | [ ] | No image-URL list |
| Summary format | `summary` | [ ] | No per-page summary mode |
| Page Q&A | `query` format, `highlights` | [ ] | No per-page query/highlight extraction |
| Location / device | `location` {country, languages}, `mobile` | [ ] | No geo/mobile emulation |
| Cache control | `maxAge`, `minAge`, `storeInCache` | [~] | Have cache; no per-call age knobs |
| PDF parsing | `parsePDF` | [x] | `read_pdf` |
| PII redaction | `redactPII` | [ ] | none |
| **Map** (URL discovery) | `/map` - fast full URL list from a site | [~] | Argus `map` is **P3-only / not built** |
| **Crawl** entire site | `/crawl` | [x] | `crawl` (Crawl4AI BFS + robots) |
| **Extract** (LLM, multi-page) | `/extract` NL + schema across pages | [~] | `extract_structured` is per-URL; not site-wide agentic extract |
| **Search** + full content | `/search` returns search results with scraped content | [~] | `research` bundle is closest |
| Change tracking | `changeTracking` format | [~] | Argus `watch` is **P3-only / not built** |

Docs: https://docs.firecrawl.dev/api-reference/introduction / https://docs.firecrawl.dev/features/scrape

</details>

### 1.4 Exa - search / contents / findSimilar / answer

<details>
<summary>Feature table</summary>

| Feature | Params | Argus? | Notes |
|---|---|:--:|---|
| Neural / semantic search | `type` = neural/auto/fast/deep/deep-reasoning/instant | [ ] | **Biggest gap.** SearXNG is keyword/lexical only |
| Keyword search | `type=keyword` | [x] | SearXNG |
| Category filter | `category` = company/research paper/news/pdf/github/personal site/people/financial report | [ ] | No semantic category targeting |
| Result count | `numResults` (1-100) | [x] | `search.count` (1-50) |
| Domain include/exclude | `includeDomains`, `excludeDomains` | [~] | doable via SearXNG `site:` but not first-class params |
| Date filters | `startPublishedDate`/`endPublishedDate`, `startCrawlDate`/`endCrawlDate` | [~] | only coarse `time_range` |
| Text include/exclude | `includeText`, `excludeText` | [ ] | none |
| Contents: full text | `contents.text` (maxCharacters, includeHtmlTags) | [x] | `read`/`research` full content |
| Highlights | `contents.highlights` {query, numSentences, highlightsPerUrl} | [ ] | No query-relevant snippet extraction |
| Summary | `contents.summary` {query, schema} | [ ] | No per-result LLM summary |
| Livecrawl | `livecrawl` = never/fallback/preferred/always | [~] | We always fetch live + cache; no cache/live policy knob |
| Subpages | `subpages`, `subpageTarget` | [ ] | No subpage expansion |
| **findSimilar** | url -> semantically similar pages | [ ] | No related/similar-page discovery |
| **answer** | NL question -> cited answer over results | [~] | `research` returns raw bundle, not a synthesized answer |
| **research** (agentic) | structured JSON + citations | [~] | `research` deep is closest, not structured/agentic |

Docs: https://exa.ai/docs/reference/getting-started / OpenAPI: https://github.com/exa-labs/openapi-spec

</details>

### 1.5 Tavily - search / extract / crawl / map

<details>
<summary>Feature table</summary>

| Feature | Params | Argus? | Notes |
|---|---|:--:|---|
| Search depth | `search_depth` basic/advanced/fast/ultra-fast | [~] | one tier; rerank gives "advanced"-like quality |
| Topic | `topic` general/news/finance | [~] | `category` general/news/science/it (no finance) |
| Answer | `include_answer` (basic/advanced) | [~] | `research` raw bundle, no synthesized answer |
| Raw content | `include_raw_content` (markdown/text) | [x] | `read` full content |
| Chunks per source | `chunks_per_source` (1-3) | [ ] | No relevance-chunked snippets |
| Images + descriptions | `include_images`, `include_image_descriptions` | [ ] | No image list / VLM descriptions |
| Time / date | `time_range`, `start_date`, `end_date`, `days` | [~] | `time_range` only; no explicit dates |
| Domain filters | `include_domains` (<=300), `exclude_domains` (<=150) | [~] | via `site:` only |
| Country boost | `country` | [ ] | none |
| Auto params | `auto_parameters`, `exact_match` | [ ] | none |
| **Extract** | `urls` (<=20), `extract_depth`, `format`, `query` rerank, `chunks_per_source` | [x] | `batch_read` (no rerank/chunking) |
| **Crawl** (graph) | `url`, `instructions` (NL), `max_depth` (<=5), `max_breadth` (<=500), `limit`, `select_paths`/`select_domains`, `allow_external`, `categories` | [~] | `crawl` has depth/include/exclude; **no NL `instructions`** |
| **Map** | site URL discovery | [~] | Argus `map` P3-only |

Docs: https://docs.tavily.com/documentation/api-reference/endpoint/search / /extract / /crawl

</details>

### 1.6 Bright Data - Web Unlocker / SERP API / proxies / Browser API / Scraper

<details>
<summary>Feature table</summary>

| Feature | Capability | Argus? | Notes |
|---|---|:--:|---|
| Web Unlocker | anti-bot bypass, CAPTCHA solving, fingerprinting, ~98% success -> clean HTML/JSON | [~] | Patchright stealth + archive fallback; **no CAPTCHA solving, no managed proxy pool** |
| SERP API | parsed Google/Bing/Yandex results | [~] | SearXNG aggregates engines (incl. Google/Bing) - self-hosted equivalent |
| Residential proxies | 400M+ IPs, 195+ countries, opt-in | [ ] | Not self-hostable economically (needs IP pool); use cheap residential add-on if ever needed |
| Datacenter / ISP / Mobile proxies | rotating pools | [ ] | same - outside self-host scope |
| Geo-targeting | country/state/city/ZIP/ASN | [ ] | tied to proxy network |
| Browser API | managed Puppeteer/Selenium/Playwright + CAPTCHA | [~] | We run our own Playwright/Patchright (self-hosted), no CAPTCHA solver |
| Web Scraper API / Datasets | 120+ site-specific scrapers, prebuilt datasets | [~] | Our trading extractors are the same idea, narrower scope (the moat) |
| Scraper Studio | AI scraper builder w/ auto-maintenance | [ ] | none |

Docs: https://docs.brightdata.com/scraping-automation/web-unlocker/introduction / https://docs.brightdata.com/proxy-networks/residential/introduction / https://brightdata.com/products

</details>

---

## 2. Missing / partial in Argus - ranked adoption list

Ranked by value x self-hostability. **(a)** what / **(b)** who has it / **(c)** self-host feasibility / **(d)** effort / **(e)** priority.

| # | Gap | (b) Competitors | (c) Self-host feasibility | (d) Effort | (e) Priority |
|--:|---|---|---|:--:|:--:|
| 1 | **`answer` mode** - NL question -> synthesized, cited answer over search->read bundle | Exa `/answer`, Tavily `include_answer`, Jina DeepSearch, Brave `summary` | [x] Yes - add summarized mode to existing `research`; reuse owned LLM (Groq/NVIDIA). No new infra | S-M | **P1.5** |
| 2 | **`map`** - fast sitemap/URL discovery for a site | Firecrawl `/map`, Tavily `/map` | [x] Yes - parse `sitemap.xml` + `robots.txt` + 1-hop link harvest. Pure stdlib/httpx | S | **P1.5** (promote from P3) |
| 3 | **Semantic / neural search + `findSimilar`** | Exa `type=neural` + `/findSimilar` | [x] Yes - local embeddings (bge/e5 via sentence-transformers or fastembed) + vector index (FAISS/sqlite-vec). Re-rank SearXNG results semantically; `findSimilar` = embed URL content -> ANN over cache/index | M-L | **P2** |
| 4 | **`highlights` / relevance snippets + chunks_per_source** | Exa highlights, Tavily chunks, Brave extra_snippets | [x] Yes - chunk content, embed query, return top-N sentences (same embedding model as #3) | S-M | **P2** |
| 5 | **Image alt-text / captioning + image-URL list** | Jina `X-With-Generated-Alt`/`X-With-Images-Summary`, Firecrawl `images`, Tavily `include_image_descriptions` | [x] Yes - image list is trivial (parse `<img>`); captioning via local VLM (Florence-2/BLIP), lazy-loaded | S (list) / M (captions) | **P2** |
| 6 | **Change-tracking / `watch`** - poll + diff -> events | Firecrawl `changeTracking`, (Argus P3) | [x] Yes - already designed P3; cron + content-hash diff -> Telegram/webhook | M | **P2/P3** (high value for trading: calendar/COT) |
| 7 | **Per-call fetch knobs** - `format=screenshot` on read, exclude-selectors, no-cache, cache max-age, UA/referer override, locale | Jina headers, Firecrawl `maxAge`/`includeTags`/`excludeTags`, location | [x] Yes - thin params over existing Playwright/cache | S | **P1.5** |
| 8 | **First-class search filters** - `include_domains`/`exclude_domains`, custom date range, country/locale, safesearch, spellcheck | Brave, Exa, Tavily | [x] Yes - SearXNG supports `site:`, safesearch, engine/lang params; surface as Argus params | S | **P1.5** |
| 9 | **Richer browser `actions`** - `write`/`press`/`executeJavascript` | Firecrawl, Bright Data Browser | [x] Yes - Playwright already supports `fill`/`press`/`evaluate`; expose them | S | **P2** |
| 10 | **`links` / `images` output formats** (deduped lists) + links-summary | Jina, Firecrawl | [x] Yes - parse + dedupe; trivial | S | **P2** |
| 11 | **Result types / infobox / FAQ / locations** | Brave rich types | [~] Partial - depends on SearXNG engine output; structured infobox is hard self-hosted | M | P3 (low) |
| 12 | **CAPTCHA solving + managed residential/geo proxies** | Bright Data, Jina `X-Proxy-Country` | (!) Not economically self-hostable (needs IP pool / solver service) | L | **Defer** - archive fallback + stealth covers most; add paid residential add-on only if a critical source blocks us |
| 13 | **Search-depth tiers / `auto_parameters` / category targeting** | Tavily, Exa | [x] Yes (depth via rerank toggle); category needs #3 embeddings | M | P3 |
| 14 | **PII redaction** | Firecrawl `redactPII` | [x] Yes - regex/Presidio pass, optional | S | P3 (low, niche) |
| 15 | **Subpages expansion** (`subpages`/`subpageTarget`) | Exa | [x] Yes - overlaps `crawl` depth=1 | S | P3 |

**Self-hostable verdict:** items 1-10 (the high-value ones) are all doable with OSS and the LLM/embedding stack Argus already plans - **no paid API needed**. Only item 12 (managed residential IP pool + CAPTCHA) is genuinely not self-hostable; defer it and rely on stealth + archive fallback, adding a metered paid residential add-on only for a proven hard-block source.

---

## 3. What Argus already matches or beats

- **Full content, zero truncation.** `read`/`research` return complete article/markdown (<=500k chars, no silent cuts) - beats Jina/Tavily/Exa which chunk, cap `maxCharacters`, or return snippets by default.
- **Unlimited & owned.** Self-hosted SearXNG + own LLM -> no per-request/credit billing (Brave/Exa/Tavily/Firecrawl/Bright Data all meter every call; Tavily even prices `advanced` at 2x credits).
- **JS render + stealth, self-hosted.** Own Playwright + Patchright stealth + auto-escalation = our Web-Unlocker/Browser-API equivalent without per-success fees.
- **Archive (egress) fallback** - built-in resilience when origin blocks or is down; none of the paid tools offer a comparable transparent fallback.
- **Content-addressed cache** across all tools - dedup + free re-reads.
- **PDF parsing** (`read_pdf`: pymupdf4llm + Docling tables/scanned) matches/beats Firecrawl `parsePDF` and most others (Tavily/Brave have none).
- **Multi-engine search** via SearXNG = a self-hosted SERP-API equivalent (Google/Bing/etc aggregated) without Bright Data SERP fees.
- **Trading moat.** `forexfactory_calendar`/`cot_report`/`news_sentiment_feed` with >=99% golden-file accuracy = domain-specific extractors Bright Data only offers as paid site-templates - and ours are tuned to the Aurix consumer shape.
- **Deep-crawl** (`crawl`, Crawl4AI BFS + robots) matches Firecrawl/Tavily crawl for the core traversal case (gap is only the NL-`instructions` convenience).

---

## Top 5 self-hostable gaps to adopt (ranked)

1. **`answer` mode (summarized `research`)** - add an LLM-synthesized, cited answer over the existing search->read bundle. Closes the single most common paid feature (Exa/Tavily/Jina DeepSearch) using the LLM we already own; small effort, big UX win.
2. **`map` (sitemap/URL discovery)** - promote from P3; just parse `sitemap.xml`/`robots.txt` + 1-hop links. Trivial, pure-stdlib, and a table-stakes feature every competitor ships.
3. **Semantic search + `findSimilar`** - local embeddings (bge/e5) + FAISS/sqlite-vec to re-rank SearXNG results and find related pages. This is Exa's core differentiator and is fully self-hostable; medium effort but high strategic value.
4. **`highlights` / relevance snippets** - chunk + embed-query + top-N sentences per result (reuses the #3 embedding model). Makes results LLM-ready and matches Exa/Tavily/Brave snippet features for small added effort.
5. **Image alt-text/captioning + image/link lists** - `<img>`/`<a>` lists are trivial; captions via a lazy local VLM (Florence-2/BLIP). Matches Jina `X-With-Generated-Alt` and Firecrawl `images`, useful for multimodal grounding, no paid API.

> Deliberately deferred (not self-hostable): managed residential/geo proxies + CAPTCHA solving (Bright Data) - covered "good enough" today by Patchright stealth + archive fallback.
