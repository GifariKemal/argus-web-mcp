# Argus - MCP Tool Specifications (I/O contracts)

Exact contracts for each MCP tool. All tools: async, SSRF-guarded (resolve-then-validate before any fetch), cache-aware (content-addressed, per-source TTL), partial-failure tolerant where batched. Errors return a structured `{error, code, detail}` - never raise to the client. Return shapes are JSON; large content respects `anthropic/maxResultSizeChars` (<=500k) instead of silent truncation.

Legend: **P1** = MVP, **P2/P3** = later phases.

---

## `read(url, format="markdown", clean=true, include_links=false, timeout=30)` - P1
URL -> clean main content.
- **in:** `url` (str, http/https only), `format`  in  {markdown,text,html}, `clean` (strip boilerplate), `include_links` (keep hyperlinks), `timeout` (s).
- **out:** `{url, final_url, status, title, content, format, metadata:{author,published,lang,site,word_count}, from_cache, render_path:"static|browser"}`.
- **backing:** httpx static -> trafilatura -> readability fallback -> Crawl4AI/Playwright if thin/JS. **No truncation.**
- **errors:** ssrf_blocked, fetch_failed, empty_content.

## `search(query, count=10, category="general", time_range=null, lang=null)` - P1
Web search via self-hosted SearXNG (unlimited).
- **in:** `query` (str|str[]), `count` (1-50, paginate SearXNG ~20/page), `category`  in  {general,news,science,it}, `time_range`  in  {day,week,month,year}, `lang`.
- **out:** `{query, results:[{title,url,snippet,engine,published?}], count, engines_used}`.
- **backing:** SearXNG `GET /search?format=json`.
- **errors:** search_backend_down, no_results.

## `read_pdf(url_or_path, pages=null, mode="text", timeout=60)` - P1
PDF -> markdown (+ tables).
- **in:** `url_or_path`, `pages` (e.g. "1-5" or null=all), `mode`  in  {text,tables,figures}.
- **out:** `{source, pages_total, pages_returned, content(markdown), tables:[...], metadata}`.
- **backing:** pymupdf4llm (fast, digital) -> Docling (quality, tables/scanned).
- **errors:** ssrf_blocked, not_pdf, parse_failed.

## `scrape(url, wait_for=null, actions=[], screenshot=false, format="markdown", timeout=45)` - P1
JS-rendered fetch + optional interactions.
- **in:** `url`, `wait_for` (css selector|ms), `actions` (e.g. [{click,sel},{scroll}]), `screenshot` (bool), `format`.
- **out:** `{url, final_url, content, format, screenshot?(base64 png), render_path:"browser"}`.
- **backing:** Crawl4AI + Playwright; stealth (Patchright) auto-escalate on bot-block.
- **errors:** ssrf_blocked, render_failed, blocked_by_antibot.

## `batch_read(urls[], concurrency=8, format="markdown", clean=true)` - P1
Parallel `read` over many URLs.
- **in:** `urls` (str[], cap ~200), `concurrency` (semaphore-bounded), + read opts.
- **out:** `{results:[{url, ok, content?, error?}], succeeded, failed}` - **partial-failure tolerant**.
- **backing:** asyncio + httpx pool + trafilatura; browser only for JS pages.

## `extract_structured(url_or_urls, schema, prompt=null, mode="auto")` - P1 (selector) / P2 (LLM)
URL(s) -> schema-validated JSON.
- **in:** `url_or_urls`, `schema` (JSON-Schema or Pydantic-like; for selector mode: field->css/xpath map), `prompt` (LLM mode hint), `mode`  in  {selector,llm,auto}.
- **out:** `{url, data:{...schema-shaped...}, valid(bool), mode_used}`.
- **backing:** parsel (CSS/XPath, deterministic, P1) -> Instructor+Pydantic over owned LLM (Groq/NVIDIA, P2). `auto` tries selector first, LLM fallback.
- **errors:** ssrf_blocked, schema_invalid, extraction_failed.

---

## Later phases
- `crawl(seed_url, depth=2, max_pages=50, include=[], exclude=[], sitemap=true)` - **P2** - Crawl4AI deep-crawl, robots-respecting -> `{pages:[read...], link_graph}`.
- `screenshot(url, format="png")` - **P2** - Playwright -> image bytes (always full-page).
- `watch(url, selector, interval, webhook)` - **P3** - poll+diff -> change events (calendar/COT -> Telegram). No paid equivalent.
- `map(url)` - **P3** - sitemap/link discovery -> URL list.

## Trading-specialized (P2, behind `read`/`extract_structured` - the moat)
- `forexfactory_calendar(date_range)` -> `[{time,currency,event,impact,actual,forecast,previous}]` - **keyed to Aurix `calendar_client` shape (replaces deprecated FMP).**
- `cot_report(report_type, date)` -> CFTC COT positioning JSON.
- `news_sentiment_feed(query, since)` -> ranked news + optional owned-LLM sentiment/surprise score.
**Golden-file tested, >=99% field accuracy gate before live Aurix use.**

## Conventions
snake_case tools/params / ISO-8601 dates / all URLs SSRF-checked / every tool has a unit test (fixtured) + integration test (local fixture server) / timeouts on every fetch / structured errors, never exceptions to client.
