# Argus - Step-by-Step Roadmap (no gaps)

> [!NOTE]
> Status (updated 2026-10-04; deployed 2026-06-25, re-provisioned 2026-09-15): P0 / P1 / P2 / P3 all DONE; **deployed LIVE** at `https://argus.gifariksuryo.xyz/mcp`. P4 (operate) is live. F1 (`ARGUS_S2_API_KEY`) closed on 2026-09-19. No owner item is open.

Build **locally first**, prove it via QA/QC + benchmark, then wrap as our MCP and deploy to the VPS. Each phase has an explicit **exit gate** - do not advance until it passes.

> [!IMPORTANT]
> Execute phases in order. Do not advance until a phase's exit gate passes.

## Contents

- [P0 - Research and Design](#p0---research--design-x-done-2026-06-24)
- [P1 - MVP, validated locally](#p1---mvp-built--validated-locally-windows-dev-box-x-done-2026-06-24)
- [P2 - Feature parity + trading moat](#p2---feature-parity-with-the-paid-tier--trading-moat-local---mostly-done-2026-06-24)
- [P3 - Productionize + deploy](#p3---productionize-the-mcp--deploy-to-vps-x-done--deployed-live-2026-06-25)
- [P4 - Operate](#p4---operate--live)

---

## P0 - Research & Design [x] DONE (2026-06-24)
- [x] Survey 12 incumbent tools (features + scoring).
- [x] 3-agent research: OSS stack, MCP/deploy engineering, feature/benchmark/QA spec.
- [x] Design doc (`docs/00-DESIGN.md`), research (`docs/01-RESEARCH.md`), this roadmap.
- [x] Project scaffold + git init.
**Exit gate:** [x] design + stack decided, docs committed.

---

## P1 - MVP, built & validated LOCALLY (Windows dev box) [x] DONE (2026-06-24)
Goal: a running FastMCP server (stdio for local dev) with the core tools, beating free competitors on the benchmark, before any server touch.

**Exit gate verified (2026-06-24):** 153 offline tests green + 2 browser + 1 slow-skipped (Docling not installed); **SSRF 100% line+branch**; core coverage 86% (>=80%); ruff clean; FastMCP boots (in-memory Client live-read of example.com/paulgraham + SSRF block confirmed). Benchmark gate **PASS** - Argus median ROUGE-L 1.000 >= best free baseline (raw_trafilatura 0.964), success 100%, truncation 1.000 on long-form.

> [!CAUTION]
> P1 gold was curated with Argus (home advantage), so the benchmark gate is *relative* (>= best free baseline), not absolute; P2 must re-curate gold independently. Local-path `read_pdf` flagged for P3 LFI lockdown.

1. **Project skeleton** - `src/argus/` Python pkg (3.11), `pyproject.toml` (deps: fastmcp, crawl4ai, trafilatura, httpx, readability-lxml, markdownify, pymupdf4llm, docling, parsel, pydantic, instructor), `uv`/venv, ruff config. `crawl4ai-setup` + `crawl4ai-doctor`.
2. **Fetch core** (`src/argus/fetch/`) - tiered: `static.py` (httpx) -> `render.py` (Crawl4AI/Playwright) -> stealth hook. Shared browser pool + asyncio.Semaphore in lifespan.
3. **Extractors** (`src/argus/extract/`) - `article.py` (trafilatura->readability->markdownify), `pdf.py` (pymupdf4llm fast / Docling quality), `structured.py` (parsel selector tier).
4. **SSRF guard** (`src/argus/security/ssrf.py`) - resolve-then-validate, private/metadata IP deny, re-pin IP, redirect re-check, scheme allowlist. **100% test coverage (hard gate).**
5. **Cache** (`src/argus/cache.py`) - content-addressed SQLite+disk, per-source TTL, store-good-only + stale-serve.
6. **MCP tools** (`src/argus/server.py`) - `read, search, read_pdf, scrape, batch_read, extract_structured`. FastMCP, run stdio locally for dev. `instructions` < 2 KB.
7. **SearXNG local** - run official Docker image on `127.0.0.1:8888`, JSON output; `src/argus/search.py` client.
8. **Tests** (`tests/`) - unit per extractor (fixtured HTML/PDF), integration per tool against a local fixture HTTP server (offline), SSRF guard suite. Coverage >=80% core / 100% SSRF.
9. **Benchmark harness** (`benchmark/`) - `testset.yaml` (30 URLs/7 categories + 10 queries + hand-curated gold extractions), uniform adapters (Argus + free tools + any keyed paid), `run_bench.py` + scorer -> `report.md` (ROUGE-L/F1, success, truncation, latency, cost).
**Exit gate (P1):** all tests green; SSRF 100%; benchmark - Argus median ROUGE-L >= best free competitor & >=0.85 on news/docs; success >=95%; truncation completeness >=0.98 on long-form. Runs clean locally via stdio.

---

## P2 - Feature parity with the paid tier + trading moat (local) - [~] MOSTLY DONE (2026-06-24)
1. `crawl` (Crawl4AI deep-crawl, robots), `screenshot`, `extract_structured` LLM tier (Instructor+Pydantic over Groq/NVIDIA). [x] built - LLM tier provider-agnostic (OpenAI-compatible base_url, default-off without key), mock-tested.
2. **Trading extractors** - `forexfactory_calendar`, `cot_report`, `news_sentiment_feed` -> JSON keyed to Aurix `calendar_client`. Golden-file tests. [x] **100% field accuracy** on real FairEconomy-FF-JSON + CFTC-COT samples (>=99% hard gate MET). (!) Aurix field-name alignment to confirm before live Aurix use.
3. Docling PDF tier for scanned/complex; anti-bot Patchright integration (lazy). [x] Docling `mode='quality'` wired + validated; Patchright stealth auto-escalation on anti-bot block.
4. Re-run benchmark vs all 12. [x] re-run with **independent** gold.

**Exit gate (P2):** trading-source field accuracy >=99% - **[x] PASS** (100%, golden-file, objective). Benchmark quality - **[x] PASS (fair metric)**: the raw-text ROUGE-L gate was confounded (rewards least-transformed output), so it was replaced by a **formatting-invariant `quality_f1`** (`content_recall` x `boilerplate_rejection` over hand-verified main-content sentences + boilerplate strings, `benchmark/quality_gold.yaml`). Result: **Argus median quality_f1 1.000 - ties the best free baseline** (raw_trafilatura/readability); a raw DOM dump tanks on rejection by design. As of v0.4.4 the active quality corpus is non-trading (`docs-01`, `longform-04`). Legacy ROUGE-L numbers retained in report, marked confounded. Objective gates also hold: 100% success, SSRF 100%, full-content (no truncation). Full report: `benchmark/report.md`.

Verified P2: 212 offline tests + 3 browser + 2 slow (Docling) green; SSRF 100%; ruff clean; 11 MCP tools registered.

### P2+ enhancements (post-exit-gate, benchmark/competitor-driven)
After the P2 gate, a 200-scenario benchmark + 3-way comparison (vs Claude Code & Codex native, n=50) + a competitor feature-gap analysis (`docs/05-COMPETITIVE-GAP.md`) drove these additions - all TDD, all green:
- **Search:** multi-engine redundancy + auto-backoff (throttle resilience), rerank v2, domain filters + safesearch, **local semantic hybrid rerank** (fastembed bge-small; quantified **+27% nDCG on conceptual queries**, A/B `benchmark/semantic_ab.py`; replaced in 0.4.21 by multilingual MiniLM, see `benchmark/semantic_id.py`).
- **New tools:** `research(deep/quick/answer)`, `map_urls`, `find_similar` (Exa-style semantic), `github_search` (repos/code/issues) -> **15 MCP tools** total.
- **Egress fallback:** stealth-browser -> Wayback archive on connect-fail/block.
- **Multi-agent QA/QC end-to-end:** 387 offline + browser + slow green; SSRF 100%; ruff clean; security Round-2 (`deploy/SECURITY-AUDIT.md`) no Critical/High; live smoke of all 15 tools (zero crashes). Repo tidied (`chore: tidy repo`).
- Durable benchmark record: `benchmark/reports/RESULTS.md`.

---

## P3 - Productionize the MCP & DEPLOY to VPS [x] DONE + DEPLOYED LIVE (2026-06-25)
Only after P1+P2 gates pass. **Local productionization + artifacts + security gate DONE (2026-06-24); deployed live to the VPS (2026-06-25).** Live at `https://argus.gifariksuryo.xyz/mcp`.
1. **Switch transport to Streamable HTTP** - [x] `app = mcp.http_app(path="/mcp")` (uvicorn `argus.server:app`); `/health` + `/metrics` (Prometheus). Auth `StaticTokenVerifier` from env `ARGUS_TOKEN`. Verified live: /health 200, /metrics OK, /mcp no-token -> 401.
2. **Deploy artifacts** (`deploy/`) - [x] `argus.service` (systemd, `User=argus`, hardened, EnvironmentFile), `argus.nginx.conf` (TLS, `proxy_buffering off`, /metrics loopback-only), `provision.sh` (idempotent, playwright-as-argus, SearXNG secret_key gen), `fail2ban-argus.conf`, `argus.env.example`, `README` runbook. **Security SAST + deps-audit done (`deploy/SECURITY-AUDIT.md`): 2 HIGH fixed/accepted, mediums/lows fixed.**
3. **Deploy to the SURIOTA VPS** - [x] **LIVE** on `<VPS_IP>` since 2026-09-15, after `103.172.172.29` died. Runs as an Easypanel Compose service off the repo's `docker-compose.yml`: `argus` (uvicorn :8090, `--workers 1`) plus `searxng` (`http://searxng:8080`), no host ports published. Traefik terminates Let's Encrypt TLS; since 2026-09-18 the domain is Cloudflare-proxied and the public router only accepts Cloudflare ranges, with the rate limit keyed on `CF-Connecting-IP`. Secrets live in the Easypanel service env (`updateComposeEnv` with `createDotEnv: true`), never in the repo. The nginx + fail2ban + systemd path is retired but kept as `deploy/provision.sh`.
4. **Auto-deploy** - [x] Since 2026-09-15 the VPS runs Argus under Easypanel and a GitHub push webhook redeploys `main`. The earlier mechanism, `deploy/argus-update.{sh,service,timer}` (poll `main` every 5 min, fast-forward only, `/health`-gate, **auto-rollback**, skip-restart on docs-only changes, mode-drift hardened), is disabled on the host and stays in the repo for the panel-less deployment path.
5. **Security + load test** - [x] security SAST + deps-audit done. [x] **local load test PASS** (`benchmark/loadtest.py`): 1000-read flood x3 rounds 0 errors + RSS flat ~181 MB (no leak); 24 concurrent browser renders x3 rounds 0 errors, peak active_contexts=4=pool concurrency (semaphore bound holds), RSS flat ~216 MB (no context leak); peak well under 2 GB cap.
6. **Register in Claude Code** - [x] `claude mcp add --transport http argus https://argus.gifariksuryo.xyz/mcp --header Authorization` (user scope); zero local process confirmed.
7. **Cutover** - point Aurix `calendar_client`/research + general web needs at Argus; optionally retire remaining web-MCP redundancy.
**Exit gate (P3):** [x] remote HTTP MCP live + authed + TLS; health green; load test passes (no OOM/leak); Claude Code connects with zero local process; security gates clean. **MET.**

---

## P4 - Operate [~] LIVE

<details>
<summary>Hardening rounds 6-10 (2026-07-02, CHANGELOG 0.4.0 - 0.4.5)</summary>

- [x] **Round-6 hardening (2026-07-02, CHANGELOG 0.4.0):** 49-agent audit -> 30 fixes shipped (cache self-heal + eviction + key-casing, throttle concurrency, render anti-wedge + challenge-page gate, crawl deadline/clamps/semaphore, 4 tools cached + degraded-never-cached, `argus_tool_errors_total` metrics, smart_search failover, trading drift detectors + coded errors, watch interval-on-error, PDF pages/magic/slice, gzip sitemaps, rerank floor backfill + query-param dedup + stopword guard, router modal-'may'). Suite 640 -> 722, coverage 94% held. Deferred: `_SEM_FLOOR` recalibration (needs live A/B).
- [x] **Round-7 hardening (2026-07-02, CHANGELOG 0.4.1):** gap-scan round 7 -> 8 verified fixes (run-aware article dedup for trafilatura whole-body dup, SSRF DNS off the event loop w/ `ARGUS_DNS_TIMEOUT`, `research()` wall-clock timeout, highlights from full pre-cap content, `search` invalid-category -> `schema_invalid`, read/scrape/screenshot `blocked_by_antibot` via `FetchError.code` + `batch_read` ok=False, recycle wedged stealth crawler). Suite 722 -> 735, coverage 94% held.
- [x] **Round-8 hardening (2026-07-02, CHANGELOG 0.4.2):** gap-scan round 8 -> 10 long-tail fixes (read/scrape/batch_read `format` enum + `search` `time_range` enum -> `schema_invalid`, static meta-charset sniff for legacy encodings, `research` highlights try/except guard, `batch_read` crash isolation, `map_site` robots-sitemap seed cap, corrupt-blob file unlink, wired `ARGUS_LOG_LEVEL`, env-var + TOOL-SPECS doc drift + drift-guard test). Suite 735 -> 746, coverage 94% held.
- [x] **Round-9 hardening (2026-07-02, CHANGELOG 0.4.3):** gap-scan round 9 -> 4 code + 6 doc fixes (low_relevance guard now credits semantic rescues on the hybrid path, `screenshot` antibot via `FetchError.code`, `map_urls` `max_urls` trust-boundary clamp, bogus-charset LookupError-fallback test; version lockstep 0.4.3, README/AGENTS/ROADMAP + env-var docs sync). Coverage 94% held.
- [x] **Round-10 benchmark/tuning (2026-07-02, CHANGELOG 0.4.4):** end-to-end benchmark follow-up -> active benchmark scope reset to non-trading by default (160 search scenarios, 40 compare IDs, 16 URL items, 8 search-query items), deterministic tool-surface benchmark added for 19 active non-trading tool boundaries, search routing/relevance rescue tightened, large-PDF fast text path and static-fallback timeout tuned. Final gates: 763 offline passed, SSRF 100%, ruff clean, browser/slow/network markers green. Live search smoke over 16 non-trading scenarios: 100% success, 0% throttle; `dev` remains flagged for overlap/degraded review.
- [x] **Round-10 final gap-scan (2026-07-02, CHANGELOG 0.4.5):** 4-agent review of a802678/v0.4.4 found 3 real fixes plus doc drift: preserve `backend_failover` through category rescue, wrap `smart_search` with final structured-error handling, make 40-id Codex merge strict by default, and sync science/rescued-category/current benchmark docs. Final gates: 767 offline passed, SSRF 100%, ruff clean, browser/slow/network markers green.

</details>

- [x] External cron curls `/health`; Prometheus `/metrics` for error-rate / active-context. (The Hermes watchdog that filled this role died with the old host in September 2026.)
- [x] The VPS stays in sync with `main` automatically (Easypanel push webhook; previously the ff-only, health-gated, auto-rollback timer).
- [x] Benchmark is a re-runnable regression gate before any future change (`benchmark/run_4way.py`, n=25 recorded).
- [x] **F1 closed (2026-09-19):** `ARGUS_S2_API_KEY` is set in the Easypanel service env. It lifts `scholar_search`'s S2 hit rate but does not remove 429s (roughly half even at 6 s spacing), so the retry budget went to 3 attempts at a 1 s backoff base and CrossRef stays the fallback.
- [x] **WARP egress (2026-10-04, CHANGELOG 0.4.23 - 0.4.25):** measured from the VPS first: Wayback 429 -> 200, Reuters 401 -> 200, WSJ 401 -> 200, FXStreet 403 -> 200 through Cloudflare WARP; search engines and S2 did not improve. A `warp` compose service carries those hosts in the httpx and browser tiers (`CONNECT <validated-ip>`, SSRF unchanged, direct fallback), Wayback gained a CDX fallback, browser DNS failures are no longer counted as SSRF blocks. Validated three ways: suite in the production image, live end-to-end and attack runs, independent review.
- [x] **Reachability map + alerts (2026-10-04, CHANGELOG 0.4.26 - 0.4.27):** `/health` reports the WARP egress and listed hosts failing through it; `uptime.yml` e-mails on either. Daily direct-vs-WARP map of 13 sites picks the next official-API adapter: reddit, medium, quora, investing, npmjs are blocked both ways.
- [x] **The everything-else batch (2026-10-15, CHANGELOG 0.4.32-0.4.33):** brief research, read cap, npm adapter, engine-dead alert, schema_invalid, Indonesian benchmark, 8-agent load test, blind LLM judge (native 15:4 on source quality) -> authority prior in the rerank. **Open:** re-run the judge after deploy; add the `engines_dead` check to `uptime.yml` from an account with workflow scope; bing is dead from the VPS (re-measure with `scripts/check_engines.py`, trim `ARGUS_SEARCH_ENGINES`); `kemenperin.go.id` to `ARGUS_EGRESS_PROXY_HOSTS`; SearXNG sends no `publishedDate`; a bigger VPS is the lever for multi-agent research latency.
- [x] **Research tail (2026-10-15, CHANGELOG 0.4.30):** first-done-wins bundle with 2 spare candidates and a 3 s rank grace; p95 21-28 s -> 8.7 s on the benchmark candidate lists, same yield. Open: SearXNG sends no `publishedDate` from the VPS (check engine config on the box); Wikipedia REST adapter; opt-in `max_chars` on `read`; LLM-judge for agent answer quality.
- [x] **Extraction tuning (2026-10-15, CHANGELOG 0.4.29):** profiled the read path on real pages; extraction moved to spawned worker processes (threads never parallelised trafilatura: GIL) and to one balanced pass (WCXB dev: F1 0.815 -> 0.817 at -39% time). Open: `_metadata` is ~30% of a page; a cheaper date/author search needs its own WCXB-style measurement.
- [x] **Production audit (2026-10-14, CHANGELOG 0.4.28, `docs/qa/2026-10-14-production-audit.md`):** security + code + performance reviews worked by measurement. Verified 2026-10-15: `ruff` clean, 1070 offline tests green, SSRF gate 100%; committed. **Still open:** push (the webhook deploys; confirm `ARGUS_TOKEN` is in the Easypanel env first), then the deferred items in the audit doc (container `cap_drop`/`pids_limit` need a Docker host to verify; B1 fetch benchmark from the VPS).
- [x] **X module Phase A (2026-10-30, CHANGELOG 0.4.35):** `x_search` + x.com `read` via FxTwitter, no account. Next: Layer 1 (one aged cookie account, GraphQL with live queryId refresh) per `docs/plans/2026-10-30-argus-x-module-design.md`.
- [~] **X module Phase B+C (2026-10-30, CHANGELOG 0.4.36-0.4.37, live with @srtargus from 0.4.37; exit gate = 7 days without a lock):** Layer 1 code (SearchTimeline with one cookie account, live manifest, vendored transaction id, pacing, fallback to the index). Spike without cookies: x.com reachable from the primary server, logged-out build detected. Phase C (live with @srtargus, 7 days without a lock) starts when the owner sets `ARGUS_X_AUTH_TOKEN`/`ARGUS_X_CT0`; the logged-in bundle scan is verified then.
- [x] **Reddit threads with comments (2026-10-30, CHANGELOG 0.4.36):** thread Atom feed before the page. medium/quora/npmjs read from the new IP; fresh investing.com articles stay blocked on every free route (Wayback for archived ones).
- [x] **Second reference study + read everything (2026-10-31, CHANGELOG 0.4.40, `docs/qa/2026-10-31-reference-study-2.md`):** 14 repos read at code level, free sources measured from the primary server. `read` handles PDF, DOCX, PPTX, XLSX, EPUB, CSV, JSON, XML and RSS/Atom feeds (zip guard, entity-free XML, worker pool); Hacker News, Polymarket and YouTube-transcript adapters; browser only where it can help; no cached error pages; URL-path dates (rank unchanged); SERP pages skipped in research; SearXNG `hackernews` engine. **Open (measure first):** cross-index agreement in the rerank (M1, `engines` now kept), rs-trafilatura on WCXB (M2), JSON data islands (M3), B1 1000-URL fetch benchmark (M4), GDELT, arctic-shift, StockTwits, tesseract OCR.
- [x] **Measure-first batch (2026-10-31, CHANGELOG 0.4.41):** M1-M13 measured from the production server and WCXB; adopted rs-trafilatura page-type routing, tesseract OCR, StockTwits; B1 1000-URL baseline recorded. **Open:** per-host browser memory (M7) needs repeated-host traffic logs.
- Phase-future (YAGNI): owned search index, 24h soak on the live box. (A rotating proxy pool is not needed: extra WARP accounts share one IPv4 exit, measured 2026-10-04.)

---

### Cross-cutting discipline (every phase)
Syntax-check -> adversarial self-review -> tests -> validate (mirrors Aurix Phase-3). No silent truncation/caps without a logged note. Version + CHANGELOG per feature. Backups before destructive ops. Document as we go - no gaps left for "later".
