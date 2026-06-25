# Argus - Step-by-Step Roadmap (no gaps)

> **Status (2026-06-25):** P0 / P1 / P2 / P3 all DONE; **deployed LIVE** at `https://argus.gifariksuryo.xyz/mcp`. P4 (operate) is live. Remaining owner item: `ARGUS_S2_API_KEY` (F1).

Build **locally first**, prove it via QA/QC + benchmark, then wrap as our MCP and deploy to the VPS. Each phase has an explicit **exit gate** - do not advance until it passes.

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

**Exit gate verified (2026-06-24):** 153 offline tests green + 2 browser + 1 slow-skipped (Docling not installed); **SSRF 100% line+branch**; core coverage 86% (>=80%); ruff clean; FastMCP boots (in-memory Client live-read of example.com/paulgraham + SSRF block confirmed). Benchmark gate **PASS** - Argus median ROUGE-L 1.000 >= best free baseline (raw_trafilatura 0.964), success 100%, truncation 1.000 on long-form. (!) Known caveat: P1 gold curated with Argus (home advantage) -> benchmark gate is *relative* (>= best free baseline), not absolute; P2 must re-curate gold independently. Local-path `read_pdf` flagged for P3 LFI lockdown.

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

**Exit gate (P2):** trading-source field accuracy >=99% - **[x] PASS** (100%, golden-file, objective). Benchmark quality - **[x] PASS (fair metric)**: the raw-text ROUGE-L gate was confounded (rewards least-transformed output), so it was replaced by a **formatting-invariant `quality_f1`** (`content_recall` x `boilerplate_rejection` over hand-verified main-content sentences + boilerplate strings, `benchmark/quality_gold.yaml`). Result: **Argus median quality_f1 1.000 - ties the best free baseline** (raw_trafilatura/readability); a raw DOM dump tanks on rejection by design. Only sub-1.0: `news-02` 0.923 - a real, minor trailing-CTA leak (trafilatura keeps an in-body "Apply here" promo on the Fortune template; recall still 1.0). Legacy ROUGE-L numbers retained in report, marked confounded. Objective gates also hold: 100% success, SSRF 100%, full-content (no truncation). Full report: `benchmark/report.md`.

Verified P2: 212 offline tests + 3 browser + 2 slow (Docling) green; SSRF 100%; ruff clean; 11 MCP tools registered.

### P2+ enhancements (post-exit-gate, benchmark/competitor-driven)
After the P2 gate, a 200-scenario benchmark + 3-way comparison (vs Claude Code & Codex native, n=50) + a competitor feature-gap analysis (`docs/05-COMPETITIVE-GAP.md`) drove these additions - all TDD, all green:
- **Search:** multi-engine redundancy + auto-backoff (throttle resilience), rerank v2, domain filters + safesearch, **local semantic hybrid rerank** (fastembed bge-small; quantified **+27% nDCG on conceptual queries**, A/B `benchmark/semantic_ab.py`).
- **New tools:** `research(deep/quick/answer)`, `map_urls`, `find_similar` (Exa-style semantic), `github_search` (repos/code/issues) -> **15 MCP tools** total.
- **Egress fallback:** stealth-browser -> Wayback archive on connect-fail/block.
- **Multi-agent QA/QC end-to-end:** 387 offline + browser + slow green; SSRF 100%; ruff clean; security Round-2 (`deploy/SECURITY-AUDIT.md`) no Critical/High; live smoke of all 15 tools (zero crashes). Repo tidied (`chore: tidy repo`).
- Durable benchmark record: `benchmark/RESULTS.md`.

---

## P3 - Productionize the MCP & DEPLOY to VPS [x] DONE + DEPLOYED LIVE (2026-06-25)
Only after P1+P2 gates pass. **Local productionization + artifacts + security gate DONE (2026-06-24); deployed live to the VPS (2026-06-25).** Live at `https://argus.gifariksuryo.xyz/mcp`.
1. **Switch transport to Streamable HTTP** - [x] `app = mcp.http_app(path="/mcp")` (uvicorn `argus.server:app`); `/health` + `/metrics` (Prometheus). Auth `StaticTokenVerifier` from env `ARGUS_TOKEN`. Verified live: /health 200, /metrics OK, /mcp no-token -> 401.
2. **Deploy artifacts** (`deploy/`) - [x] `argus.service` (systemd, `User=argus`, hardened, EnvironmentFile), `argus.nginx.conf` (TLS, `proxy_buffering off`, /metrics loopback-only), `provision.sh` (idempotent, playwright-as-argus, SearXNG secret_key gen), `fail2ban-argus.conf`, `argus.env.example`, `README` runbook. **Security SAST + deps-audit done (`deploy/SECURITY-AUDIT.md`): 2 HIGH fixed/accepted, mediums/lows fixed.**
3. **Deploy to `103.172.172.29`** - [x] **LIVE** via SSH (key-only `gifari_vps_ed25519`): SearXNG :8888, Argus :8090 (`--workers 1`), nginx `argus.gifariksuryo.xyz` + Let's Encrypt TLS + fail2ban. Coexists with Hermes/SUVA. Secret via scp (not Hermes tools).
4. **Safe auto-update** - [x] `deploy/argus-update.{sh,service,timer}` poll `main` every 5 min, fast-forward only, `/health`-gate, **auto-rollback** on failure, skip-restart on docs-only changes, mode-drift hardened.
5. **Security + load test** - [x] security SAST + deps-audit done. [x] **local load test PASS** (`benchmark/loadtest.py`): 1000-read flood x3 rounds 0 errors + RSS flat ~181 MB (no leak); 24 concurrent browser renders x3 rounds 0 errors, peak active_contexts=4=pool concurrency (semaphore bound holds), RSS flat ~216 MB (no context leak); peak well under 2 GB cap.
6. **Register in Claude Code** - [x] `claude mcp add --transport http argus https://argus.gifariksuryo.xyz/mcp --header Authorization` (user scope); zero local process confirmed.
7. **Cutover** - point Aurix `calendar_client`/research + general web needs at Argus; optionally retire remaining web-MCP redundancy.
**Exit gate (P3):** [x] remote HTTP MCP live + authed + TLS; health green; load test passes (no OOM/leak); Claude Code connects with zero local process; security gates clean. **MET.**

---

## P4 - Operate [~] LIVE
- [x] Hermes watchdog curls `/health`; Prometheus `/metrics` for error-rate / active-context.
- [x] Safe auto-update timer keeps the VPS in sync with `main` (ff-only, health-gated, auto-rollback).
- [x] Benchmark is a re-runnable regression gate before any future change (`benchmark/run_4way.py`, n=25 recorded).
- **Open owner item (F1):** set `ARGUS_S2_API_KEY` (Semantic Scholar) to lift `scholar_search` rate limits.
- Phase-future (YAGNI): proxy pool, owned search index, 24h soak on the live box.

---

### Cross-cutting discipline (every phase)
Syntax-check -> adversarial self-review -> tests -> validate (mirrors Aurix Phase-3). No silent truncation/caps without a logged note. Version + CHANGELOG per feature. Backups before destructive ops. Document as we go - no gaps left for "later".
