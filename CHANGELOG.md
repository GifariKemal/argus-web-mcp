<div align="center">

# CHANGELOG - Argus

<img src="https://img.shields.io/badge/format-Keep_a_Changelog-2dd4bf?style=flat-square" alt="Keep a Changelog"/>
<img src="https://img.shields.io/badge/tools-20-22c55e?style=flat-square" alt="20 tools"/>
<img src="https://img.shields.io/badge/tests-700+-3fb950?style=flat-square" alt="700+ tests"/>
<img src="https://img.shields.io/badge/status-LIVE-16a34a?style=flat-square" alt="live"/>
<img src="https://img.shields.io/badge/created-2026--06--24-0ea5e9?style=flat-square" alt="created"/>

</div>

All notable changes, in [Keep a Changelog](https://keepachangelog.com/) style. Dates are absolute (`YYYY-MM-DD`). Argus went from research to a 20-tool, security-audited, benchmarked, **publicly-deployed** MCP server in two intensive days (2026-06-24 build, 2026-06-25 deploy + tuning); early entries are grouped by build phase rather than calendar day.

---

## [0.4.7] - 2026-07-14 - Observability, compression

Make every fallback visible (so future tuning is data-driven) and shrink what goes
over the wire and onto disk. All additive; no behavior change to a healthy request.

### Added

- **Pipeline-stage observability.** Each fetch-ladder hop and search fallback now
  increments a stage counter exported at `/metrics` as `argus_pipeline_stage_total{stage=...}`
  (`fetch.static_ok`, `fetch.static_fail`, `fetch.fallback_stealth_ok/fail`,
  `fetch.fallback_archive_ok`, `fetch.fallback_exhausted`, `fetch.thin_escalate*`,
  `fetch.forced_browser`, `search.engine_benched`, `search.backend_failover`,
  `search.low_relevance`). Shows which fallback fires most - the durable signal for
  tuning the tiers without grepping logs.
- **Per-step logging.** The same hops log at INFO on any fallback/escalation and at
  DEBUG for the happy path (raise `ARGUS_LOG_LEVEL=DEBUG` for the full per-step trace).

### Changed

- **Cache blobs are gzip-compressed on disk.** Full-page content compresses ~5-10x, so
  the on-disk blob store stays small on the VPS. The read path auto-detects gzip vs a
  legacy plain blob, so existing cache entries keep working with no migration.
- **nginx gzip for tool responses.** `application/json` + text responses are compressed
  over the wire (engages only when the client sends `Accept-Encoding: gzip`; works with
  `proxy_buffering off`). Cuts bandwidth on the large full-content bundles.

### Pruned

- Repo-wide over-engineering audit: no dead code, hand-rolled stdlib, or unused config
  found in `src/argus` (prior rounds already trimmed it). No churn.

## [0.4.6] - 2026-07-14 - Live-log-driven resilience pass

Improvements from a 7-day production journald audit (search-engine throttling, timeout
long-tails, benign teardown noise). All additive; no behavior change to a healthy request.

### Added

- **Client-side per-engine cooldown (`search`).** When SearXNG reports an engine
  `unresponsive` (rate-limited/CAPTCHA on a datacenter IP), Argus benches it for a window
  (`ARGUS_ENGINE_COOLDOWN`, default 120s) so the next general fan-out stops requesting it and
  concentrates on engines that answer. Complements SearXNG's server-side `suspended_times`;
  cuts the dominant log signal (~1200 "engines unresponsive" events/7d) and wasted sub-requests.
  Safety floor: never benches below 2 fan-out engines.

### Fixed

- **`scrape` wall-clock now bounded by its own `timeout`.** The normal->stealth escalation ran
  two renders (each up to `timeout + grace`), so a scrape could take ~2x its configured timeout
  (observed p99 ~184s at a 90s setting). Wrapped in an outer `asyncio.timeout(timeout)` ->
  structured `fetch_failed` "scrape timed out", matching the `research`/`crawl` pattern.
- **Client-inflated `timeout` clamped to the server ceiling.** `timeout` is a tool parameter, so
  a caller could pass `timeout=900` and blow past the intended bound (a likely cause of the
  `research` p99 ~858s tail). The metrics middleware now clamps every tool's `timeout` arg to
  `TIMEOUTS[name]` before dispatch (a caller may request less, never more).

### Changed

- **Log hygiene.** A loop exception handler demotes known-benign async-teardown tracebacks
  (client-disconnect `ClosedResourceError`, browser `net::ERR_ABORTED` / detached-frame futures)
  to a single debug line. Real errors still pass through to the default handler untouched.

## [0.4.5] - 2026-07-02 - Round 10 final gap-scan

Final gap-scan over the deployed a802678/v0.4.4 tree, focused on code added during
the benchmark reset/search/PDF tuning pass.

### Fixed

- **`search()` preserves `backend_failover` degradation through category rescue.** A primary
  backend failure followed by low-relevance fallback results and a successful routed rescue could
  previously clear `degraded`, causing fallback results to look clean and become cacheable. Rescue
  now clears only pure `low_relevance`; failover stays surfaced and uncached. Rescue against an
  external fallback backend also uses the SSRF-safe client path.
- **`smart_search()` now obeys the tool error contract.** Invalid non-string/empty queries return
  `schema_invalid`, and unexpected internal exceptions return structured `search_backend_down`
  instead of leaking across the MCP boundary.
- **3-way merge now requires complete Codex coverage by default.** The active compare set is 40 IDs;
  `merge-3way` fails fast when Codex output files are missing unless `--allow-partial-codex` is
  passed explicitly. Active Codex output path is now `benchmark/codex_compare/`.

### Docs / Benchmark

- Synced `smart_search` docs/instructions with the `science` route and documented
  `search.rescued_category`.
- Removed stale trading seams/text from the active tool-surface benchmark.
- Corrected status/date/benchmark-gate drift in `CLAUDE.md`, `docs/02-ROADMAP.md`, and
  `benchmark/reports/RESULTS.md`.

### Tested

- `ruff check src tests benchmark`
- Offline suite: 767 passed, 8 deselected.
- SSRF gate: 45 passed, 100% line + branch coverage for `argus.security.ssrf`.
- Browser marker: 3 passed. Slow marker: 3 passed. Network marker: 2 passed.
- Tool-surface smoke: 19/19 active non-trading boundaries OK.

## [0.4.4] - 2026-07-02 - Benchmark scope reset + search/PDF tuning

Follow-up benchmark pass after the end-to-end Argus stress work. The active benchmark
surface is now non-trading by default, while runtime trading tools remain available and
tested separately.

### Changed

- **Benchmark scope reset:** removed active trading/MQL5 scenarios from `benchmark/scenarios.py`,
  `benchmark/testset.yaml`, `benchmark/quality_gold.yaml`, burst tests, and the deterministic
  tool-surface benchmark. Active search scenarios are now 160 non-trading queries across 8
  categories; `COMPARE_IDS` is now 40 IDs. Active extraction testset is 16 URL items + 8 search
  queries. Removed the stale `gold/longform-01.md` market/investment gold reference.
- **Added deterministic tool-surface benchmark** (`benchmark/run_tool_surface.py`) over 19 active
  non-trading server tool boundaries using local fixtures only. Run artifacts are ignored under
  `benchmark/_runs/`.
- **Search relevance tuning:** default SearXNG language now resolves to English unless overridden,
  semantic low-relevance guarding is stricter, science routing is explicit, and weak general
  searches can rescue into routed categories (`science`, `it`, `news`) before being marked degraded.
- **PDF/read latency tuning:** large text PDFs take a fast PyMuPDF text path, and static fetches cap
  timeout earlier when browser fallback is available.

### Tested

- `ruff check src tests benchmark`
- Offline suite: 763 passed, 8 deselected.
- SSRF gate: 45 passed, 100% line + branch coverage for `argus.security.ssrf`.
- Browser marker: 3 passed. Slow marker: 3 passed. Network marker: 2 passed.
- Benchmark smoke: tool-surface non-trading 19/19 OK; quality benchmark 2/2 items at
  `quality_f1=1.000`; live search smoke over 16 non-trading scenarios hit 100% success and 0%
  throttle, with `dev` still flagged for low overlap/degraded review.

## [0.4.3] - 2026-07-02 - Gap-scan round 9: convergence + doc sync

A 17-agent workflow (4 subsystem deep-dives + 2 comprehensive doc-drift audits + synthesis +
adversarial verify) scanned the post-0.4.2 tree. Convergence continues - 4 small code fixes + a
full documentation sync. Suite 746 -> 753 passed, ruff clean, coverage 94% held.

### Fixed

- **`search()` low_relevance guard no longer false-flags semantic rescues.** The guard recomputed
  pure-lexical overlap even on the hybrid path, so a legitimate paraphrase set (zero lexical overlap
  but high cosine - exactly what the hybrid blend rescues) was wrongly flagged `degraded=low_relevance`.
  `_rerank_hybrid` now tags each row's semantic relevance (transient, stripped before return) and the
  guard credits a row as relevant on lexical overlap OR `cosine >= _SEM_FLOOR`. Genuine junk (low
  cosine AND no lexical overlap) still flags. Lexical-only path unchanged.
- **`screenshot()` now surfaces `blocked_by_antibot` via `FetchError.code`** (round 7 converted read/
  scrape but missed screenshot; the old `"antibot" in str(e)` substring never matched the real
  `"...(anti-bot block)"` message, so the branch was dead).
- **`map_urls` clamps `max_urls`** at the trust boundary (`max(1, min(max_urls, 5000))`, like crawl/
  find_similar) - a negative value previously dropped the last URLs and misreported `truncated=True`.

### Tested

- Guard-credits-semantic-rescue + still-flags-low-cosine-junk (hybrid path); screenshot antibot code;
  map_urls clamp (low + high); bogus meta-charset -> utf-8 LookupError fallback.

### Docs / chore

- **Version lockstep**: `pyproject.toml` + `argus.__version__` bumped `0.1.0 -> 0.4.3` (they had drifted
  from the CHANGELOG/tag through every release).
- **README + AGENTS** test-count badges/text corrected `722 -> 753`.
- **ROADMAP** P4 log now records rounds 7, 8, 9 (was Round-6 only).
- **`deploy/argus.env.example`** documents the remaining real env vars: `ARGUS_S2_API_KEY` /
  `SEMANTIC_SCHOLAR_API_KEY` (scholar rate-limit) and `ARGUS_HEALTH_LATENCY_BUCKETS` (/metrics buffer).
- **TOOL-SPECS** timeout literals + env-var docs are now in sync with the code (audited end-to-end).

## [0.4.2] - 2026-07-02 - Gap-scan round 8: 10 long-tail fixes

A 27-agent workflow (8 subsystem deep-dives + 3 SOTA-research + synthesis + adversarial
verify-per-finding) scanned the post-0.4.1 tree. After two prior audits the remaining gaps
are lower-severity but real; all shipped with offline regression tests. Suite 735 -> 746
passed, ruff clean, coverage 94% held. (Two verified trading-only fixes were intentionally
dropped - the trading tools are not in use here; and an opt-in `max_chars` cap for read/scrape
was deferred as a feature, not a gap.)

### Fixed

- **read()/scrape()/batch_read() silently coerced an out-of-enum `format` to markdown** while
  echoing the bogus label (and wasting a fetch); batch_read would then KeyError on the err dict.
  Now reject with `schema_invalid`, consistent with the read_pdf/category guards.
- **search() silently ignored an out-of-enum `time_range`** (SearXNG returns all-time results
  for a non-`{day,week,month,year}` value). Now `schema_invalid`.
- **Static fast-path mojibake'd meta-only legacy encodings** - httpx defaults to utf-8 with no
  header charset, corrupting windows-1251/shift_jis/etc. pages irreversibly. Now decodes with the
  header charset when present, else sniffs a `<meta>`/`<?xml>`-declared charset before utf-8.
- **research() highlights ran outside try/except** - a runtime embedding failure turned a
  successful bundle into an uncaught MCP error. Now guarded (skip highlights + log).
- **batch_read had no crash isolation** - an unexpected exception in one `read()` sank the whole
  batch. Now `gather(return_exceptions=True)` + per-URL failure normalization.
- **map_site fetched robots.txt `Sitemap:` directives uncapped**, bypassing `_MAX_CHILD_SITEMAPS`.
  The robots-derived seed list is now capped too.
- **Corrupt-blob cache self-heal leaked the blob file** - it deleted the DB row but left the
  orphaned file on disk. Now unlinks the blob as well.

### Changed / docs

- **`ARGUS_LOG_LEVEL` is now wired** (sets the `argus` logger level at import) - it was documented
  but read nowhere. The two truly-dead knobs `ARGUS_REQUEST_TIMEOUT` / `ARGUS_BROWSER_TIMEOUT` are
  removed from the deploy docs (the real knobs are the per-tool `ARGUS_TIMEOUT_*`).
- **Documented the real, previously-undocumented env vars** in `deploy/argus.env.example`: JWT auth
  (`ARGUS_JWT_JWKS_URI`/`ISSUER`/`AUDIENCE`), `ARGUS_GITHUB_TOKEN`, `ARGUS_COURTESY_DELAY`,
  `ARGUS_MIN_CONTENT_WORDS`.
- **Fixed 5 stale timeout defaults in `docs/03-TOOL-SPECS.md`** to match `config.TIMEOUTS`, and added
  a `test_config` guard that fails on future doc/config timeout drift.

## [0.4.1] - 2026-07-02 - Gap-scan round 7: 8 verified fixes

A 30-agent workflow (9 subsystem deep-dives + 4 SOTA-research + synthesis + adversarial
verify-per-finding) scanned the post-0.4.0 tree; 8 findings survived review, all with an
offline before/after and all shipped with regression tests. Suite 722 -> 735 passed, ruff
clean, coverage 94% held.

### Fixed

- **Whole-body article duplication** - `_dedup_blocks` was adjacent-only, so trafilatura
  2.x's verbatim re-emit of the entire `<article>`/`<main>` body (a contiguous run repeated
  right after itself: `[Title, A, B, A, B]`) was never collapsed, doubling returned tokens
  on well-structured pages for `read`/`research`/`scrape`/`crawl`. Now run-aware (collapses
  the longest adjacent run-duplication; single-block repeat is the `L==1` case); genuine
  non-adjacent refrains are preserved.
- **`scholar_search(open_access=True)` returned no_results on the CrossRef fallback** -
  `_map_crossref` hardcoded `open_access_pdf=None`, dropping every result on the common
  anonymous-S2-429 path. Now maps the first `application/pdf` entry from CrossRef's `link`
  array (URL-guarded).
- **`research()` had no overall wall clock** - the `timeout` arg bounded only per-source
  fetches, so sequential backfill waves could run ~3x the stated budget. Wrapped in
  `asyncio.timeout(timeout)` (mirroring `crawl`) -> structured `fetch_failed` on overrun.
- **Highlights were computed after truncation** - with `highlights=True` +
  `max_chars_per_source`, `top_sentences` ran over the already-capped prefix, so a top
  query-relevant sentence past the cap could never surface. Now computed from the full
  pre-cap content (stashed then always stripped, so the payload stays lean either way).
- **`search(category=...)` silently coerced an invalid enum to `general`** (and cached the
  wrong-scope result). Now rejects with `schema_invalid` up front, consistent with
  `read_pdf` / `extract_structured`.
- **`read()` collapsed `blocked_by_antibot` into `fetch_failed`** unlike `scrape`/
  `screenshot`, and all three used a `"antibot" in str(e)` message check that never matched
  the real message (`"...(anti-bot block)"` - hyphenated). All three now derive the code from
  the structured `FetchError.code`; `batch_read` counts an antibot block as `ok=False`.
- **A wedged stealth browser was reused until process restart** - `_bounded_arun` bounded the
  wedge (0.4.0) but kept `_stealth` pointed at the hung crawler. Now recycles it (close+null
  under lock) on timeout so the next call re-inits a fresh one (stealth tier only; the normal
  tier has no lazy re-init).

### Changed (perf)

- **SSRF DNS resolution now runs off the event loop, bounded by a timeout.** `resolve_and_validate`
  called blocking `socket.getaddrinfo` synchronously from 6 async paths (incl. the safe-transport
  send hook, which re-resolves per request AND per redirect hop); on the single worker one slow/hung
  lookup froze ALL concurrent tool calls. New `aresolve_and_validate` runs the same validator via
  `asyncio.to_thread` under `asyncio.timeout(ARGUS_DNS_TIMEOUT`, default 5`)`, re-raising a timeout as
  `SSRFError`. Security logic byte-for-byte identical (validation, IP-pinning, both defence-in-depth
  re-resolves unchanged). New `ARGUS_DNS_TIMEOUT` documented in `deploy/argus.env.example`.

## [0.4.0] - 2026-07-02 - Hardening round 6: multi-agent audit, 30 fixes shipped

A 49-agent workflow (7 module-group analyzers + one adversarial verifier per finding) audited the whole codebase; 41/42 findings survived adversarial review. Everything offline-measurable was shipped, each with regression tests: suite 640 -> 722 passed, ruff clean, coverage total 94% held (touched modules at or above baseline). Root-caused, not symptom-patched.

### Fixed - correctness

- **Cache: missing/corrupt blob no longer raises into tools** - a deleted or truncated blob file (disk cleanup, crash mid-write) made every cached tool throw for the whole TTL and `get_stale` fail forever. Now self-heals: dead row deleted, treated as a cache miss, fresh fetch follows.
- **Cache: `key()` no longer lowercases path/query** - `read("https://host/API")` and `/api` collided onto one cache key and served each other's content for up to an hour. Only scheme+host are case-insensitive per RFC 3986. One-time cold cache for mixed-case keys.
- **Throttle: per-host courtesy delay now holds under concurrency** - N same-host acquirers (batch_read fires 8) all read the stale `last_request` and burst simultaneously after one shared sleep. Slot reservation (write before await) queues them at exactly `min_interval` spacing.
- **Render: challenge pages can no longer masquerade as content** - a success=True "Just a moment..." page (either tier) now raises `blocked_by_antibot` instead of feeding "Verify you are human" into read/scrape/research; fetch core then falls through to its static/Wayback ladder.
- **Render: wedged Chromium cannot starve the pool** - `arun` had no outer bound; a hung CDP pipe held a semaphore permit forever (4 hangs = browser tier dead until restart). `asyncio.timeout(timeout + 15s grace)` converts it to a bounded `render_failed`.
- **Rerank: safety floor backfills instead of replacing** - when relevant results were a minority, the floor branch REPLACED them with the backend's first-N junk; relevant tail hits now always survive (lexical + hybrid paths).
- **Rerank: URL dedup keeps meaningful query params** - `watch?v=AAA` vs `?v=BBB` no longer collapse as duplicates; tracking params (`utm_*`, fbclid, gclid, ...) still dedup; params compare order-insensitively.
- **Relevance guard ignores stopword overlap** - garbage sharing only "to"/"the" with a natural-language query is now flagged `low_relevance` (guard-only change; rerank scoring untouched).
- **Router: modal "may" no longer routes to news** - "what may cause a memory leak" went to the news backend. "may" now only counts as a month when date-anchored.
- **research: per-source isolation restored** - one unexpected extractor error killed the whole deep bundle; now an isolated `{url, error: "extract_failed"}` entry.
- **extract_structured (auto): no more bare `None`** - selector tier raising with no LLM fallback returned `None` to the client; now a structured `extraction_failed` error.
- **PDF: pages-spec errors are honest** - malformed ("abc", "5-") or fully out-of-document specs on a VALID PDF returned `not_pdf`/crash paths; now `schema_invalid`. Reversed ranges auto-swap. Unknown `read_pdf` mode -> `schema_invalid` (docs advertised a nonexistent `figures`).
- **PDF: spec-legal leading junk accepted** - `%PDF-` may sit up to 1024 bytes in (naive proxies/CGI prepend junk; pymupdf parses these fine); the magic gate now searches the first KiB.
- **PDF quality tier honors `pages`** - Docling OCR'd the WHOLE document while reporting `pages_returned` as if sliced; the PDF is now sliced via pymupdf before Docling.
- **map_urls: gzipped sitemaps decompressed** - `.xml.gz` payloads (standard on WordPress/news/large sites) arrived as mojibake and were silently skipped, collapsing discovery to 1-hop links. Magic-byte sniff + decompressed-size cap (zip-bomb guard).
- **Article extractor: only ADJACENT duplicate blocks collapse** - the global dedup deleted legitimate non-consecutive repeats (refrains, repeated legal clauses), contradicting its own docstring.
- **ForexFactory: non-dict feed elements skipped** - one junk element crashed the whole calendar with AttributeError.
- **ForexFactory: unknown impact labels pass through verbatim** - previously folded into "Holiday", hiding a potentially high-impact event class on feed drift; empty still -> "Holiday".
- **ForexFactory: `date_range` validated** - malformed bounds ("2026-6-2", ints, 1-element) raise coded `ff_bad_date_range` instead of silently mis-filtering.
- **watch: failing sources honor `interval_s`** - an errored check never advanced `last_check`, so a broken URL was re-fetched EVERY 60s tick forever (60x load for a 1h watch). Errors now advance the clock, keep the baseline hash, and surface in poll results; a persist OSError on one watch no longer aborts the rest of the tick.
- **crawl: real deadline + real error contract** - `ARGUS_TIMEOUT_CRAWL` (180s) was dead config and `deep_crawl`'s `timeout` param was never read: a tarpit crawl could hold the shared Chromium ~50 minutes. Now: per-page `page_timeout`, whole-crawl `asyncio.timeout` at the tool layer, `depth`/`max_pages` clamped (0-5 / 1-200), crawler exceptions -> structured `fetch_failed` (dead `CrawlError` class removed), and the crawl holds a BrowserPool permit so its page loads count against the RAM guard.
- **cot_report: `date` honored, error codes unmasked** - `date` was silently ignored (wrong-week data served as requested); now filters rows (`requested_date` echoed, non-matching -> honest empty set). `cot_bad_report_type`/`ff_*` codes reach the client instead of being flattened to `fetch_failed`.

### Added

- **Cache eviction** - `Cache.purge(max_age_s=7d)` deletes expired rows + their blobs and sweeps orphaned blob files (a shrink-re-put leaked the old blob forever); runs hourly from the watch loop. Unbounded `~/.argus` growth on the shared VPS (Hermes/SUVA co-tenant) is now bounded.
- **4 uncached tools now cached** - read_pdf (URL only; `pdf` TTL 24h - repeated Docling parses drop from seconds to ms), forexfactory_calendar + cot_report (`trading` 300s), news_sentiment_feed (`news` 900s). These TTLs existed as dead config since P1. Stale-fallback FF bundles are never cached.
- **Degraded results are never cached** - a `low_relevance`/failover search, degraded research bundle, incomplete GitHub scan, or degraded news feed retries next call instead of re-serving junk for the full TTL.
- **`argus_tool_errors_total{code=...}` on /metrics** - errors return as dicts (never raise), so Prometheus previously saw an SSRF block or a dead SearXNG as SUCCESS. Now alertable per error code.
- **smart_search specialist failover** - a dead/rate-limited GitHub/scholar backend (anon 10 req/min) falls back to general search, flagged `degraded: true` + `specialist_failover` instead of returning a dead error.
- **github_search surfaces `incomplete_results`** - GitHub's partial-index-scan flag now maps to the project-wide `degraded`/`degraded_reason` convention.
- **news_sentiment_feed propagates `degraded`** - off-topic or failed-over news is no longer served as clean trading input.
- **COT live drift detectors** - `identity_failures` (composed accounting identity per row) + `bad_dates` (ISO check) on every response; a CFTC column-layout change now lights up live instead of only in the offline golden test.
- **watch poller logging** - the server poll loop logs failures (was a bare `pass`).

### Changed

- **Article metadata extraction ~4x cheaper** - `_metadata` used full `bare_extraction` (re-parses the entire body) for 5 header fields; now `extract_metadata` (measured 1.50 -> 0.38 ms/call on the ad-heavy fixture, identical field values). Hot path of read/scrape/batch_read/crawl/research.
- docs/03-TOOL-SPECS.md refreshed: search/smart_search/github degraded fields, read_pdf modes + caching + `schema_invalid`, crawl `timeout`+clamps, trading contracts (drift detectors, coded errors, cache TTLs), map_urls gzip note.

### Deferred (explicitly)

- `_SEM_FLOOR` recalibration + semantic-rescue/guard alignment: they change rerank scores, so they are gated on the live semantic A/B harness (SearXNG + LLM judge) - not shippable on unit tests alone.
- Proxy pool, LLM tier default-on, multi-worker uvicorn: owner decisions, unchanged by design.

## [0.3.2] - 2026-07-02 - Relevance guard: majority rule

Live dogfooding surfaced a second shape of the same failure the 0.3.1 guard was meant to catch. Root cause confirmed by querying SearXNG directly on the box: on the datacenter IP every quality engine is CAPTCHA/rate-limit **suspended** (brave, duckduckgo, google, mojeek, qwant, startpage) leaving **bing as the sole responder**, and bing under throttle returns generic filler (AOL.com pages for a "kanban orchestration" query; "affect vs effect" grammar pages for a "Nous Research Hermes" query) that SearXNG parses as results. The 0.3.1 guard only fired on **zero** token overlap, so a set where one filler page incidentally shared a single query word slipped through with `degraded: false` (observed in `research` deep mode: Microsoft Copilot/Windows docs returned for a Hermes query).

### Changed

- **Relevance guard is now majority-based.** `search()` flags `degraded: true` + `degraded_reason: "low_relevance"` when **fewer than half** the returned results share a title/snippet token with the query (was: only when *no* result overlapped). A lone incidental token match no longer masks an otherwise off-topic set. On-topic sets (the overwhelming majority overlap) are untouched; the flag remains advisory (nothing dropped, nothing errored).

### Tested

- New regression: majority-off-topic set with one incidental single-token match -> `degraded=true`. Existing zero-overlap and on-topic cases still hold. Full suite green (640 passed).

### Root cause (NOT fixed here - needs an infra decision)

The garbage originates upstream: a single datacenter IP gets CAPTCHA'd by all the good engines, so only a throttled bing survives and serves filler. The detection above makes Argus **honest** about it, but the elimination is the outbound **proxy pool** already scaffolded (commented) in `deploy/searxng/settings.yml` (`outgoing.proxies`) - route SearXNG's engine requests through rotating residential/socks5 proxies so the majors stop throttling. Alternatively, a SearXNG image bump may refresh the bing scraper. Both are owner/cost decisions, left to the operator.

## [0.3.1] - 2026-07-02 - Relevance guard

Follow-up to the concurrency investigation: under parallel `read` + `research` load, SearXNG occasionally returned an entirely off-topic result set (observed: Google Drive pages for a Hermes query), which `search`/`research` surfaced with `degraded: false` - silent garbage. The `search` params path is concurrency-safe (per-call `{**params, ...}`, thread-safe httpx client); the defect was the *absence of a signal* that the returned set was unrelated. Fix is a deterministic, defense-in-depth relevance guard - no behavior change for on-topic queries.

### Added

- **Low-relevance guard in `search()`** - after rerank, if the query has usable tokens (>=2 chars) and **no** returned result shares a title/snippet token with it, the response is flagged `degraded: true` with new field `degraded_reason: "low_relevance"`. Lets the consuming agent (Hermes/Claude Code) retry or discount the batch instead of trusting off-topic hits.
- **`degraded_reason`** field on `search()` responses (`null` when clean; `"backend_failover"` when a fallback backend served the query; `"low_relevance"` per above).

### Fixed

- **`research()` now propagates the search `degraded`/`degraded_reason`** into every bundle (quick/deep/answer), so a low-relevance or failover signal is no longer swallowed by the research layer.

### Tested

- New regression tests: off-topic result set -> `degraded=true` + `low_relevance`; on-topic set stays clean; `research` surfaces the propagated `degraded`. Full suite green (639 passed).

## [0.3.0] - 2026-07-02 - Evidence-based tuning

Multi-agent analysis (14-agent workflow: 7 code deep-dive + 6 external-SOTA research + synthesis) then a 3-agent adversarial review (0 blocking), producing conservative, tested, benchmark-informed tuning. All changes deployed live and reversible. Confirmed already-good (not gaps): hybrid rerank is auto-on and live; the LLM tier is deliberately off by design.

### Added

- **`ARGUS_SEMANTIC_RERANK` env knob** (`auto`|`on`|`off`, default `auto`) - ops kill-switch / in-prod A-B lever over the hybrid semantic rerank (A/B-validated +14.3% nDCG@5, +27.3% on conceptual). `auto` (unset/unknown too) preserves today's behavior: hybrid iff the local embedding stack loads. Documented in `deploy/argus.env.example`.
- **`ARGUS_ENABLE_LLM` documented** in `deploy/argus.env.example` - the previously-undocumented REQUIRED gate for the LLM tier (a key alone never enabled it); clarifies the fail-safe, tools-not-brain design.

### Changed

- **Anti-bot status blocks now escalate** - `fetch_static` raises `FetchError` on HTTP 403/429/503 so `fetch.core` fires the existing stealth-browser + Wayback fallback ladder. Previously a WAF/Cloudflare challenge page (a non-2xx with a body) was returned as if it were content, because the recovery ladder is gated on `except FetchError` and a status block never raised - so it never fired. Makes the static tier consistent with the browser tier's pre-existing block heuristic.
- **Article extraction drops comment threads** - `trafilatura.extract(..., include_comments=False)` on both the markdown and text paths, so Reddit/HN/Disqus comment blocks no longer leak into main content (higher boilerplate rejection, no recall loss on articles).
- **SearXNG penalty box shortened** (`deploy/searxng/settings.yml` `suspended_times`: CAPTCHA 24h->15m, TooManyRequests 1h->5m, AccessDenied 24h->15m) - a single throttled burst no longer benches an engine for hours. Root-cause fix for the DuckDuckGo answer-concentration (was 189/200): keeps the multi-engine fan-out populated so rerank sees a diverse pool. Private loopback instance (`limiter: false`).

### Fixed

- **`research()` throttle bypass** - `research`/`_deep_bundle`/`_read_one` now thread the per-host `HostThrottle`, and the `research` server tool passes `throttle=s.throttle` (every other fetch tool already did). A deep-research call no longer fires parallel same-host fetches with zero courtesy delay and no circuit-breaker - a politeness/reliability defect flagged in `benchmark/reports/RESULTS.md`.

### Tested

- New regression tests: parametrized 403/429/503 static block -> stealth-browser escalation; `research(throttle=X)` forwards the throttle into fetch. Full suite green.

### Follow-ups (deferred, non-blocking)

- 429 escalates without honoring `Retry-After` (consider special-casing vs 403/503); block-escalation widens browser-render load on the single worker (watch read/research p95). Larger deferred items (per the tuning plan): curate forum/PDF benchmark gold, Docling PDF-quality fix, optional cross-encoder rerank / curl_cffi TLS tier.

## [0.2.0] - 2026-06-25 - DEPLOYED LIVE

Live at `https://argus.gifariksuryo.xyz/mcp` on VPS `103.172.172.29` (uvicorn `127.0.0.1:8090 --workers 1`, SearXNG `:8888`, Let's Encrypt TLS, fail2ban). Surfaced and fixed by live end-to-end testing of the deployed MCP.

### Added

- **Safe VPS auto-update** (`deploy/argus-update.sh` + `.service` + `.timer`) - pull-only (no inbound port) poll of `main` every 5 min, **fast-forward only**, reinstall deps only on manifest change, restart, `/health`-gate, and **auto-rollback** to the prior commit on failure. Hardened against mode-drift; **skips restart on docs-only changes**. Runbook in [`deploy/README.md`](deploy/README.md).
- **Cache WAL** - SQLite write-ahead logging for concurrent-reader durability under load.

### Changed

- **research (deep mode)** - `MIN_CONTENT_WORDS=30` low-content floor moves near-empty stub pages (e.g. a bare video page) to `failed` as `low_content`; **source backfill** keeps pulling from the overfetched candidate pool until `max_sources` GOOD sources or the pool is exhausted (failures no longer shrink the bundle; the happy path does no extra fetches). Added `max_chars_per_source` to bound per-source payload.
- **scholar_search** - retry Semantic Scholar on HTTP 429 (2x bounded backoff) so the richer S2 backend is used; **citation-rerank** by query/title overlap then citation count, so the canonical highly-cited paper beats derivative "X is All You Need" titles.
- **search** - Docker/generic-token tuning plus a gentle relative-relevance gate (`_REL_FLOOR=0.25`) that trims clearly-weak backfill (off-topic single-generic-token matches) without hurting recall or the `_MIN_KEEP=3` floor; consistent across the lexical and hybrid paths.

### Fixed

- **Stealth race** - resolved a concurrency race in the stealth-browser escalation path.

### Benchmarked

- **4-way harness** (`benchmark/run_4way.py`) + **n=25** head-to-head results recorded; `research()` runs **3-6s in-process** (Argus is not the bottleneck; observed CLI latency is agent + transport, not the server). See [`benchmark/reports/RESULTS.md`](benchmark/reports/RESULTS.md).

### Security / QA

- Security + cleanup + coverage round: **600 offline tests** green (plus browser + slow); SSRF 100%; ruff clean.

### Docs

- Status refresh across `CHANGELOG`, `docs/00-DESIGN.md`, and `docs/02-ROADMAP.md` to reflect the live deployment and 20-tool surface.

---

## [0.1.0] - 2026-06-24 - feature-complete build (20 tools)

The full local build: research to a 20-tool, security-audited, benchmarked FastMCP server, productionized with deploy artifacts.

### Added - tools (6 -> 20)

- `smart_search` - deterministic query-to-domain auto-router (no LLM).
- `scholar_search` - structured academic search (Semantic Scholar -> CrossRef).
- `github_search` - structured GitHub repos/code/issues.
- `map_urls` - sitemap/robots/link URL discovery.
- `find_similar` - local-embedding semantic similarity (Exa-style).
- `research(deep/quick/answer)` - one-shot research bundles + `highlights`.
- `watch` / `list_watches` / `unwatch` - poll -> diff -> webhook monitoring.
- `read(extract_media)` - links + images extraction.
- `extract_structured` LLM/auto tier (optional).

### Added - capability

- **Local semantic search** (`semantic.py`, fastembed bge-small, ONNX, no torch) -> hybrid rerank (**+27% nDCG on conceptual queries**, A/B `benchmark/semantic_ab.py`) + `find_similar` + highlights.
- **Egress fallback** - stealth browser -> Wayback archive on connect-fail / anti-bot.
- **Search resilience** - multi-engine redundancy, auto-backoff on throttle, rerank v2, domain filters + safesearch.

### Hardening & ops

- **Streaming body-size cap** - `fetch/static.py` aborts a chunked / no-Content-Length body once it exceeds 32 MB (a true OOM guard, not just the header check).
- **Per-host throttle + circuit breaker** - `fetch/throttle.py`: courtesy delay between same-host requests + closed -> open -> half-open breaker; wired into `fetch()` (default-off in tests).
- **Caching everywhere** - `research` / `scholar_search` / `github_search` / `map_urls` now cache (store-good-only, per-source TTL) alongside `read` / `search`.
- **`/metrics`** - per-tool request counters via a signature-safe FastMCP middleware.
- **Auth** - `JWTVerifier` (prod) takes precedence over `StaticTokenVerifier` (dev) via env.
- **LLM is opt-in and off by default** (`ARGUS_ENABLE_LLM`) - Argus is tools-not-brain; the consuming agent synthesizes.

### Benchmarked

- 200-scenario Argus run + 3-way head-to-head vs Claude Code and Codex native (n=50): discovery parity, Argus wins on full-content depth (~7k words/query). See [`benchmark/reports/RESULTS.md`](benchmark/reports/RESULTS.md).
- Competitor feature-gap analysis -> adopted the self-hostable gaps. See [`docs/05-COMPETITIVE-GAP.md`](docs/05-COMPETITIVE-GAP.md).

### Security / QA

- Multi-agent QA/QC end-to-end: 526 offline + browser + slow green; SSRF 100%; ruff clean.
- Security audit Round 1 + 2 ([`deploy/SECURITY-AUDIT.md`](deploy/SECURITY-AUDIT.md)): no Critical / High; fixes applied (kwarg bug, prompt-injection hardening, public-suffix scope, never-raise catch-alls, embedder lock).
- Repo tidied; `.gitignore` consolidated.

### Docs

- Rich `README.md` (banner + architecture SVG, shields badges, typing animation), `SOUL.md`, `AGENTS.md`, this `CHANGELOG.md`.

---

## Build phases (P0 -> P3)

<details>
<summary>Phase-by-phase build record</summary>

### P3 - productionize + deploy

- Streamable-HTTP transport (`uvicorn argus.server:app`), `/health` + `/metrics`, bearer/JWT auth.
- `read_pdf` local-path LFI locked down (`ARGUS_ALLOW_LOCAL_PDF`, default-off on remote).
- Deploy artifacts: `argus.service`, `argus.nginx.conf`, `provision.sh`, `fail2ban-argus.conf`, [`deploy/README.md`](deploy/README.md).
- Local load test passed (no OOM/leak). Deployed live 2026-06-25 (see `[0.2.0]`).

### P2 - feature parity + trading moat

- `crawl`, `screenshot`, `extract_structured` LLM tier, Docling PDF tier, Patchright stealth.
- Trading extractors `forexfactory_calendar` / `cot_report` / `news_sentiment_feed` - **100% golden-file field accuracy** (>=99% gate).
- Benchmark quality gate moved to a formatting-invariant `quality_f1` (raw-text ROUGE-L was confounded) - Argus ties the best free baseline.

### P1 - MVP, validated locally

- 6 tools: `read / search / read_pdf / scrape / batch_read / extract_structured`.
- **SSRF guard at 100% coverage** (hard gate), content-addressed cache, tiered fetch (httpx -> Crawl4AI), SearXNG, FastMCP stdio.

### P0 - research and design

- 12-tool incumbent survey, OSS stack decision, design + roadmap + tool specs + benchmark testset.

</details>

---

<div align="center"><sub>SURIOTA / self-hosted / unlimited / owned</sub></div>
