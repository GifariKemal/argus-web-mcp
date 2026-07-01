<div align="center">

# CHANGELOG - Argus

<img src="https://img.shields.io/badge/format-Keep_a_Changelog-2dd4bf?style=flat-square" alt="Keep a Changelog"/>
<img src="https://img.shields.io/badge/tools-20-22c55e?style=flat-square" alt="20 tools"/>
<img src="https://img.shields.io/badge/tests-600+-3fb950?style=flat-square" alt="600+ tests"/>
<img src="https://img.shields.io/badge/status-LIVE-16a34a?style=flat-square" alt="live"/>
<img src="https://img.shields.io/badge/created-2026--06--24-0ea5e9?style=flat-square" alt="created"/>

</div>

All notable changes, in [Keep a Changelog](https://keepachangelog.com/) style. Dates are absolute (`YYYY-MM-DD`). Argus went from research to a 20-tool, security-audited, benchmarked, **publicly-deployed** MCP server in two intensive days (2026-06-24 build, 2026-06-25 deploy + tuning); early entries are grouped by build phase rather than calendar day.

---

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

- **`research()` throttle bypass** - `research`/`_deep_bundle`/`_read_one` now thread the per-host `HostThrottle`, and the `research` server tool passes `throttle=s.throttle` (every other fetch tool already did). A deep-research call no longer fires parallel same-host fetches with zero courtesy delay and no circuit-breaker - a politeness/reliability defect flagged in `benchmark/RESULTS.md`.

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

- **4-way harness** (`benchmark/run_4way.py`) + **n=25** head-to-head results recorded; `research()` runs **3-6s in-process** (Argus is not the bottleneck; observed CLI latency is agent + transport, not the server). See [`benchmark/RESULTS.md`](benchmark/RESULTS.md).

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

- 200-scenario Argus run + 3-way head-to-head vs Claude Code and Codex native (n=50): discovery parity, Argus wins on full-content depth (~7k words/query). See [`benchmark/RESULTS.md`](benchmark/RESULTS.md).
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
