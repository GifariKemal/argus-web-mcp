# Argus - MCP Tool Specifications (I/O contracts)

Exact contracts for each MCP tool. All tools: async, SSRF-guarded (resolve-then-validate before any fetch), cache-aware (content-addressed, per-source TTL), partial-failure tolerant where batched. Errors return a structured `{error, code, detail}` - never raise to the client. Return shapes are JSON; large content respects `anthropic/maxResultSizeChars` (<=500k) instead of silent truncation.

20 live tools (source of truth: `src/argus/server.py` `TOOLS` tuple): read, search, smart_search, read_pdf, scrape, batch_read, extract_structured, crawl, screenshot, research, map_urls, find_similar, github_search, scholar_search, watch, list_watches, unwatch, forexfactory_calendar, cot_report, news_sentiment_feed.

> [!NOTE]
> Every tool is SSRF-guarded and returns full content (no silent truncation). Errors come back as `{error, code, detail}` - tools never raise to the client.

## Contents

- [`read`](#readurl-formatmarkdown-cleantrue-include_linksfalse-extract_mediafalse-timeout60)
- [`search`](#searchquery-count10-categorygeneral-time_rangenull-langnull-include_domainsnull-exclude_domainsnull-safesearch0)
- [`smart_search`](#smart_searchquery-count10)
- [`read_pdf`](#read_pdfurl_or_path-pagesnull-modetext-timeout90)
- [`scrape`](#scrapeurl-wait_fornull-actionsnull-screenshotfalse-formatmarkdown-timeout90)
- [`batch_read`](#batch_readurls-concurrency8-formatmarkdown-cleantrue)
- [`extract_structured`](#extract_structuredurl_or_urls-schema-promptnull-modeauto)
- [`crawl`](#crawlseed_url-depth2-max_pages50-includenull-excludenull-same_domaintrue-respect_robotstrue-timeout180)
- [`screenshot`](#screenshoturl-timeout60)
- [`research`](#researchquery-modedeep-max_sources5-highlightsfalse-max_chars_per_sourcenull-timeout120)
- [`map_urls`](#map_urlsurl-max_urls500-include_subdomainstrue)
- [`find_similar`](#find_similarurl_or_text-count10)
- [`github_search`](#github_searchquery-moderepositories-languagenull-sortnull-orderdesc-limit10)
- [`scholar_search`](#scholar_searchquery-limit10-year_fromnull-open_accessfalse)
- [`watch`](#watchurl-webhook-interval_minutes60-selectornull)
- [`list_watches`](#list_watches)
- [`unwatch`](#unwatchwatch_id)
- [Trading-specialized](#trading-specialized-behind-readextract_structured---the-moat)
- [Conventions](#conventions)

---

## `read(url, format="markdown", clean=true, include_links=false, extract_media=false, timeout=60)`
URL -> clean main content.
- **in:** `url` (str, http/https only), `format`  in  {markdown,text,html}, `clean` (strip boilerplate), `include_links` (keep hyperlinks), `extract_media` (also return the page's links + images lists), `timeout` (s).
- **out:** `{url, final_url, status, title, content, format, metadata:{author,published,lang,site,word_count}, render_path:"static|browser", from_cache}` (+ `links`, `images` when `extract_media`).
- **backing:** httpx static -> trafilatura -> readability fallback -> Crawl4AI/Playwright if thin/JS. **No truncation.**
- **errors:** ssrf_blocked, fetch_failed, empty_content.

## `search(query, count=10, category="general", time_range=null, lang=null, include_domains=null, exclude_domains=null, safesearch=0)`
Web search via self-hosted SearXNG (unlimited).
- **in:** `query` (str|str[]), `count` (1-50, paginate SearXNG ~20/page), `category`  in  {general,news,science,it}, `time_range`  in  {day,week,month,year}, `lang`, `include_domains`/`exclude_domains` (str[] allow/deny), `safesearch` (0|1|2).
- **out:** `{query, results:[{title,url,snippet,engine,published?}], count, engines_used, backend, degraded, degraded_reason, rescued_category?, from_cache}`. `degraded: true` + `degraded_reason` in {`low_relevance`, `backend_failover`} signal an untrustworthy/failed-over result set (advisory - nothing dropped); degraded sets are never cached. `rescued_category` is set when a weak broad general search is rerun through a routed category (`science`/`it`/`news`).
- **backing:** SearXNG `GET /search?format=json`.
- **errors:** search_backend_down, no_results.

## `smart_search(query, count=10)`
Auto-route a query to the best backend (deterministic classifier, no LLM): github / scholar / science / news / it / general; calls the matched tool.
- **in:** `query` (str), `count`.
- **out:** `{query, route, reason, result}` where `result` is the wrapped tool's normal return shape. If a specialist backend errors (`no_results`/`search_backend_down`, e.g. GitHub anonymous rate limit), smart_search falls back to general `search` and returns `route:"general"` + `degraded: true`, `degraded_reason:"specialist_failover"`.
- **backing:** `router.classify` -> `github_search` / `scholar_search` / `search(category=science|news|it|general)`.

## `read_pdf(url_or_path, pages=null, mode="text", timeout=90)`
PDF -> markdown (+ tables).
- **in:** `url_or_path` (http/https; local paths gated by `ARGUS_ALLOW_LOCAL_PDF=1` - LFI guard on remote), `pages` (e.g. "1-5" or null=all), `mode`  in  {text,tables,quality} (unknown mode -> schema_invalid); `quality` routes to Docling for scanned/complex and honors `pages` (the PDF is sliced before Docling).
- **out:** `{source, ...result}` (result carries pages_total/pages_returned, content (markdown), tables, metadata).
- **backing:** pymupdf4llm (fast, digital) -> Docling (`mode="quality"`: tables/scanned). 64 MiB byte cap. URL fetches cached (`pdf` TTL 24h, + `from_cache`); local paths never cached.
- **errors:** ssrf_blocked, fetch_failed, not_pdf, parse_failed, schema_invalid (bad mode / malformed or out-of-document `pages`).

## `scrape(url, wait_for=null, actions=null, screenshot=false, format="markdown", timeout=90)`
JS-rendered fetch + optional interactions.
- **in:** `url`, `wait_for` (css selector|ms), `actions` (e.g. [{click,sel},{scroll}]), `screenshot` (bool), `format`.
- **out:** `{url, final_url, content, format, screenshot?(base64 png), render_path:"browser"}`.
- **backing:** Crawl4AI + Playwright; stealth (Patchright) auto-escalate on bot-block.
- **errors:** ssrf_blocked, render_failed, blocked_by_antibot.

## `batch_read(urls[], concurrency=8, format="markdown", clean=true)`
Parallel `read` over many URLs.
- **in:** `urls` (str[], cap 200), `concurrency` (semaphore-bounded), + read opts.
- **out:** `{results:[{url, ok, content?, title?, error?}], succeeded, failed}` (+ `note` if capped) - **partial-failure tolerant**.
- **backing:** asyncio + httpx pool + trafilatura; browser only for JS pages.

## `extract_structured(url_or_urls, schema, prompt=null, mode="auto")`
URL(s) -> schema-validated JSON.
- **in:** `url_or_urls` (str|str[]), `schema` (non-empty field map; selector mode: field->css/xpath; llm mode: field->type), `prompt` (LLM mode hint), `mode`  in  {selector,llm,auto}.
- **out:** single url -> `{url, data:{...schema-shaped...}, valid(bool), mode_used:"selector|llm"}`; multiple urls -> `{results:[...]}` (per-url same shape or error).
- **backing:** parsel (CSS/XPath, deterministic) -> LLM over an owned endpoint (`mode="llm"`/fallback in `auto`; needs `ARGUS_LLM_API_KEY`/`OPENAI_API_KEY` + `ARGUS_ENABLE_LLM`). `auto` tries selector first, LLM fallback only if selectors come back invalid and an LLM is available.
- **errors:** ssrf_blocked, schema_invalid, extraction_failed, fetch_failed.

## `crawl(seed_url, depth=2, max_pages=50, include=null, exclude=null, same_domain=true, respect_robots=true, timeout=180)`
Deep-crawl a site (robots-respecting, confined to the seed host by default).
- **in:** `seed_url`, `depth` (clamped to 0-5), `max_pages` (clamped to 1-200), `include`/`exclude` (URL pattern lists), `same_domain` (bool), `respect_robots` (bool), `timeout` (s; whole-crawl wall clock, default `ARGUS_TIMEOUT_CRAWL`=180).
- **out:** crawl bundle (`{pages:[...], link_graph}`-shaped) from `deep_crawl`.
- **backing:** Crawl4AI deep-crawl + Playwright (browser tier required).
- **errors:** ssrf_blocked, fetch_failed, render_failed.

## `screenshot(url, timeout=60)`
Full-page PNG screenshot of a JS-rendered page.
- **in:** `url`, `timeout`.
- **out:** `{url, final_url, screenshot(base64 png), format:"png"}`.
- **backing:** Playwright, always full-page.
- **errors:** ssrf_blocked, render_failed, blocked_by_antibot.

## `research(query, mode="deep", max_sources=5, highlights=false, max_chars_per_source=null, timeout=120)`
Deep research in one call (search -> read -> consolidate).
- **in:** `query`, `mode`  in  {deep,quick,answer}: `deep` = search + parallel FULL read of top sources -> consolidated complete content; `quick` = ranked hits (title/url/snippet) only, zero fetches; `answer` = cited LLM answer (needs LLM tier, off by default). `max_sources` (deep), `highlights` (deep: attach top query-relevant sentences per source via local embeddings), `max_chars_per_source` (opt-in cap per source; truncation FLAGGED with `truncated=true`+`full_chars`, `word_count` preserved; default null = FULL content), `timeout`.
- **out:** deep -> `{query, sources:[{url,title,content,word_count, truncated?, full_chars?, highlights?}], ...}`; quick -> ranked hits; answer -> cited answer. `from_cache` on success.
- **backing:** `search` -> parallel `read`; optional semantic highlights; optional LLM (answer mode).
- **errors:** schema_invalid (bad mode), no_results, search_backend_down, extraction_failed.

## `map_urls(url, max_urls=500, include_subdomains=true)`
Discover a site's URLs via sitemap.xml / robots.txt / 1-hop links (no full fetch).
- **in:** `url`, `max_urls`, `include_subdomains` (bool).
- **out:** URL-list bundle from `map_site` (+ `from_cache`).
- **backing:** sitemap/robots parse (gzipped `.xml.gz` sitemaps transparently decompressed, zip-bomb capped) + 1-hop link discovery (no render).
- **errors:** ssrf_blocked, fetch_failed.

## `find_similar(url_or_text, count=10)`
Find pages semantically similar to a URL's content or a text snippet (local embeddings, no API). Argus's Exa-`findSimilar` equivalent.
- **in:** `url_or_text` (a URL is fetched+extracted; otherwise treated as the seed text), `count`.
- **out:** `{seed, results:[{title,url,snippet,...,score}], count}` - ranked by cosine similarity.
- **backing:** seed embed -> SearXNG candidates -> fastembed rerank. Needs the `[semantic]` extra.
- **errors:** ssrf_blocked, fetch_failed, no_results, search_backend_down, extraction_failed (incl. missing `[semantic]`).

## `github_search(query, mode="repositories", language=null, sort=null, order="desc", limit=10)`
Structured GitHub search - repos / code / issues with stars/language/sort. Complements `search(category="it")`.
- **in:** `query`, `mode`  in  {repositories,code,issues}, `language`, `sort`, `order`  in  {asc,desc}, `limit`. `code` mode needs `GITHUB_TOKEN`; optional token raises rate limits.
- **out:** structured GitHub result bundle (`{results:[...], total_count, degraded, degraded_reason, ...}`, + `from_cache`). `degraded: true` + `degraded_reason:"incomplete_results"` when GitHub's search timed out server-side (partial index scan); degraded results are never cached.
- **backing:** GitHub Search API.
- **errors:** search_backend_down, no_results, schema_invalid.

## `scholar_search(query, limit=10, year_from=null, open_access=false)`
Structured academic-paper search (Semantic Scholar -> CrossRef fallback). Free, no key (optional S2 key).
- **in:** `query`, `limit`, `year_from`, `open_access` (bool).
- **out:** `{results:[{title,authors,year,venue,citations,doi,abstract,open_access_pdf?}], ...}` (+ `from_cache`).
- **backing:** Semantic Scholar -> CrossRef fallback.
- **errors:** search_backend_down, no_results.

## `watch(url, webhook, interval_minutes=60, selector=null)`
Register a watch: poll `url` (optionally a CSS/XPath `selector`) and POST a change event to `webhook` (e.g. Telegram). Webhook is SSRF-guarded at delivery. No paid equivalent.
- **in:** `url`, `webhook` (both SSRF-checked at register), `interval_minutes` (floored to 60s), `selector`.
- **out:** `{id, url, selector, interval_s, webhook}`. A failing source is retried on its own `interval_s` (never every poller tick); errors keep the previous baseline hash.
- **errors:** ssrf_blocked, fetch_failed.

## `list_watches()`
List registered watches.
- **out:** `{watches:[{id,url,selector,interval_s,webhook,...}], count}`.
- **errors:** fetch_failed.

## `unwatch(watch_id)`
Remove a watch by id.
- **in:** `watch_id`.
- **out:** `{id, removed(bool)}`.
- **errors:** fetch_failed.

---

## Trading-specialized (behind `read`/`extract_structured` - the moat)

> [!IMPORTANT]
> Golden-file tested, >=99% field accuracy gate before live Aurix use.

<details>
<summary>forexfactory_calendar / cot_report / news_sentiment_feed contracts</summary>

- `forexfactory_calendar(date_range=null)` -> ForexFactory economic calendar (FairEconomy JSON feed) in **Aurix `calendar_client` shape** (field-map `time->date`, `event->name`; replaces deprecated FMP). Unknown non-empty impact labels pass through verbatim (feed drift stays visible); `date_range` bounds are validated ISO dates. Cached (`trading` TTL 300s) except stale-fallback bundles. Errors: ff_bad_date_range, ff_bad_feed, ff_fetch_failed, fetch_failed.
- `cot_report(report_type="legacy_futures", date=null)` -> CFTC Commitments of Traders positioning JSON + live drift detectors `identity_failures`/`bad_dates` (>0 = treat as degraded data) + `requested_date` echo when `date` filters rows (non-matching date -> honest empty set). Cached (`trading` TTL 300s). Errors: cot_bad_report_type, cot_fetch_failed, fetch_failed.
- `news_sentiment_feed(query, since=null, sentiment=false)` -> ranked news feed (+ optional owned-LLM sentiment score when `sentiment=true`); propagates `degraded`/`degraded_reason` from the search layer. Cached (`news` TTL 900s) unless degraded. Errors: no_results, search_backend_down, extraction_failed.

</details>

## Conventions
snake_case tools/params / ISO-8601 dates / all URLs SSRF-checked / every tool has a unit test (fixtured) + integration test (local fixture server) / timeouts on every fetch / structured errors, never exceptions to client.
