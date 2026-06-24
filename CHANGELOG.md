<div align="center">

# CHANGELOG - Argus

<img src="https://img.shields.io/badge/format-Keep_a_Changelog-2dd4bf?style=flat-square" alt=""/>
<img src="https://img.shields.io/badge/tools-6_%E2%86%92_20-22c55e?style=flat-square" alt=""/>
<img src="https://img.shields.io/badge/created-2026--06--24-0ea5e9?style=flat-square" alt=""/>

</div>

All notable changes. Dates are absolute. Argus went from research -> 20-tool, security-audited, benchmarked MCP server in a single intensive build (2026-06-24); entries below are grouped by phase rather than calendar day.

---

## [Unreleased] - feature-complete locally, deploy pending

### Quality tuning (branch tune/argus-quality-bench-deploy, PR #1) - 2026-06-24
Surfaced by live end-to-end testing of the deployed MCP; focus on deep-research power.
- **scholar**: retry Semantic Scholar on 429 (2x bounded backoff) so the richer S2 backend is used; rerank by query/title overlap then citations so the canonical highly-cited paper beats derivative "X is All You Need" titles.
- **research (deep mode)**: `MIN_CONTENT_WORDS=30` low-content floor moves near-empty stub pages (e.g. a bare video page) to `failed` as `low_content`; **source backfill** keeps pulling from the overfetched candidate pool until `max_sources` GOOD sources or pool exhausted (failures no longer shrink the bundle; happy path does no extra fetches).
- **search**: gentle relative-relevance gate (`_REL_FLOOR=0.25`) trims clearly-weak backfill (off-topic single-generic-token matches) without hurting recall or the `_MIN_KEEP=3` floor; consistent across lexical + hybrid paths.
- 18 new tests; 544 offline green; ruff clean; SSRF 100% intact.

### Safe auto-update (branch tune/argus-quality-bench-deploy)
- `deploy/argus-update.sh` + `.service` + `.timer`: pull-only (no inbound port) poll of `main` every 5 min, fast-forward only, reinstall deps only on manifest change, restart, `/health`-gate, and **auto-rollback** to the prior commit on failure. Runbook in `deploy/README.md`.

### Hardening & ops
- **Streaming body-size cap** - `fetch/static.py` aborts a chunked/no-Content-Length body once it exceeds 32 MB (true OOM guard, not just the header check).
- **Per-host throttle + circuit breaker** - `fetch/throttle.py`: courtesy delay between same-host requests + closed->open->half-open breaker; wired into `fetch()` (default-off in tests).
- **Caching everywhere** - `research`/`scholar_search`/`github_search`/`map_urls` now cache (store-good-only, per-source TTL) alongside `read`/`search`.
- **`/metrics`** - per-tool request counters via a signature-safe FastMCP middleware.
- **Auth** - `JWTVerifier` (prod) takes precedence over `StaticTokenVerifier` (dev) via env.
- **LLM is opt-in & off by default** (`ARGUS_ENABLE_LLM`) - Argus is tools-not-brain; the consuming agent synthesizes.

### Added - tools (6 -> 20)
- `smart_search` - deterministic query->domain auto-router (no LLM).
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
- **Egress fallback** - stealth browser -> Wayback archive on connect-fail/anti-bot.
- **Search resilience** - multi-engine redundancy, auto-backoff on throttle, rerank v2, domain filters + safesearch.

### Benchmarked
- 200-scenario Argus run + 3-way head-to-head vs Claude Code & Codex native (n=50): discovery parity, Argus wins on full-content depth (~7k words/query). `benchmark/RESULTS.md`.
- Competitor feature-gap analysis -> adopted the self-hostable gaps. `docs/05-COMPETITIVE-GAP.md`.

### Security / QA
- Multi-agent QA/QC end-to-end: 526 offline + browser + slow green; SSRF 100%; ruff clean.
- Security audit Round 1 + 2 (`deploy/SECURITY-AUDIT.md`): no Critical/High; fixes applied (kwarg bug, prompt-injection hardening, public-suffix scope, never-raise catch-alls, embedder lock).
- Repo tidied; `.gitignore` consolidated.

### Docs
- Rich `README.md` (banner + architecture SVG, shields badges, typing animation), `SOUL.md`, `AGENTS.md`, this `CHANGELOG.md`.

---

## P3 - productionize (local complete; VPS deploy gated)
- Streamable-HTTP transport (`uvicorn argus.server:app`), `/health` + `/metrics`, bearer/JWT auth.
- `read_pdf` local-path LFI locked down (`ARGUS_ALLOW_LOCAL_PDF`, default-off on remote).
- Deploy artifacts: `argus.service`, `argus.nginx.conf`, `provision.sh`, `fail2ban-argus.conf`, `deploy/README.md`.
- Local load test passed (no OOM/leak). **Deploy awaits owner inputs** (subdomain/DNS, token, optional proxy).

## P2 - feature parity + trading moat
- `crawl`, `screenshot`, `extract_structured` LLM tier, Docling PDF tier, Patchright stealth.
- Trading extractors `forexfactory_calendar` / `cot_report` / `news_sentiment_feed` - **100% golden-file field accuracy** (>=99% gate).
- Benchmark quality gate moved to a formatting-invariant `quality_f1` (raw-text ROUGE-L was confounded) - Argus ties the best free baseline.

## P1 - MVP, validated locally
- 6 tools: `read / search / read_pdf / scrape / batch_read / extract_structured`.
- **SSRF guard at 100% coverage** (hard gate), content-addressed cache, tiered fetch (httpx -> Crawl4AI), SearXNG, FastMCP stdio.

## P0 - research & design
- 12-tool incumbent survey, OSS stack decision, design + roadmap + tool specs + benchmark testset.

<div align="center"><sub>SURIOTA / self-hosted / unlimited / owned</sub></div>
