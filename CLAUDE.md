# CLAUDE.md - Argus Web MCP

> Auto-loaded by Claude CLI in this directory. Argus = SURIOTA's **self-hosted, unlimited, owned** web **fetch / scrape / search** MCP server. Sibling to the Hermes AI Server. _Created 2026-06-24._

## What this is & scope
A FastMCP (Python) server, deployed remote-HTTP on the SURIOTA VPS, that every Claude Code CLI connects to over HTTP (**zero local client process**). Replaces paid tools (Jina/Firecrawl/Exa/Tavily/Bright Data) with self-hosted OSS -> unlimited, free, full-content, owned.

**GENERAL-PURPOSE (owner directive):** Argus serves **ALL SURIOTA domains** - firmware/ESP32 (ESP-IDF docs), trading/Aurix (ForexFactory/COT/macro), web/Flutter/Node, business/market research, AI servers - **AND broad general web research** (the "12 use cases": search, read, scrape, research, news, docs, PDF). Trading extractors are a *specialized moat*, NOT the only purpose. Don't narrow it to trading.

## Start here (read before coding)
0. [`AGENTS.md`](AGENTS.md) - how to work here (commands, hard gates, conventions). [`SOUL.md`](SOUL.md) - identity/principles. [`README.md`](README.md) - showcase. [`CHANGELOG.md`](CHANGELOG.md) - history.
1. `docs/00-DESIGN.md` - architecture, stack, MCP tools, deploy, security.
2. `docs/02-ROADMAP.md` - step-by-step phases P0->P4 + exit-gate per phase. **Execute in order.**
3. `docs/03-TOOL-SPECS.md` - MCP tool I/O contracts.
4. `docs/04-REFERENCES.md` - OSS study guide (Crawl4AI/SearXNG/FastMCP/trafilatura - what to learn + URLs).
5. `docs/01-RESEARCH.md` - research findings + sources. `benchmark/testset.yaml` - benchmark set.

## Status (updated 2026-07-02; deployed 2026-06-25)
P0/P1/P2/P3 DONE. **DEPLOYED LIVE** at `https://argus.gifariksuryo.xyz/mcp` (bearer auth) on VPS `103.172.172.29` (uvicorn `127.0.0.1:8090 --workers 1`, SearXNG docker `:8888`, LE TLS, nginx, fail2ban). Safe auto-update timer polls `main` every 5 min (ff-only -> health-gate -> auto-rollback; skips restart for docs/benchmark-only commits). Run locally: `./.venv/Scripts/python.exe -m argus.server` (stdio) or `uvicorn argus.server:app` (HTTP). Plans in `docs/plans/`.

**Built & verified:** 20 MCP tools; **767 offline tests (+browser, +slow, +network) green; SSRF 100%; ruff clean**; coverage 94%. Latest: 0.4.5 Round-10 final gap-scan (2026-07-02) fixed search failover+rescue degradation, `smart_search` structured-error handling, strict 40-id Codex merge coverage, and doc drift. Active benchmark scope is non-trading by default (160 search scenarios, 40 compare IDs, 16 URL items, 8 search queries), and deterministic tool-surface benchmark covers 19 active non-trading tool boundaries. Runtime trading tools remain available and separately tested. HTTP transport live (`/health`,`/metrics` per-tool counters, JWT/static bearer -> /mcp 401 without token). Deploy artifacts in `deploy/` (systemd/nginx/provision/fail2ban + SECURITY-AUDIT). Local load test passed (no OOM/leak). Multi-agent QA/QC end-to-end clean (security Round-2: no Critical/High). Hardening: streaming body-cap, per-host throttle+circuit-breaker, archive egress-fallback, full caching.

**No LLM needed (architecture):** Argus is tools-not-brain - the consuming agent (Claude Code Opus 4.8 / Codex) does synthesis from `research(deep)`/`quick` raw-content bundles. Argus's LLM tier (research `answer`, extract_structured `llm`) is OPTIONAL, **off by default** (requires `ARGUS_ENABLE_LLM=1` + endpoint), so Argus is fully functional with zero LLM. Not a deploy dependency.

**Hard gates:** SSRF 100% [x] / trading field-accuracy 100% (golden-file) [x] / benchmark quality via formatting-invariant `quality_f1` - Argus ties best free baseline [x] / semantic rerank quantified **+27% nDCG on conceptual queries** [x].

**Competitive position (historical benchmark vs Claude Code & Codex native, n=50; active non-trading compare set now n=40):** discovery parity, Argus wins on full-content depth (~7k words/query), freshness, owned/unlimited. Adopted competitor features: `map_urls`, `research(answer)`, `find_similar` (Exa-style semantic), domain filters, `github_search`.

**OPEN (owner inputs):** **F1** - set `ARGUS_S2_API_KEY` in `/etc/argus/argus.env` (free Semantic Scholar signup) so `scholar_search` uses the richer S2 backend instead of the CrossRef fallback. Optional/durable: SearXNG `outgoing.proxies` (free engines throttle per-IP on a datacenter IP under bursts; the broadened engine set mitigates). `read_pdf` local-path LFI stays disabled on remote (`ARGUS_ALLOW_LOCAL_PDF` unset). **No LLM needed** - leave `ARGUS_ENABLE_LLM` unset; the consuming agent synthesizes. Aurix calendar field-map (`time->date`,`event->name`) noted in `trading/forexfactory.py`.

## Stack (decided)
Crawl4AI (Apache-2.0, clone&improve core) / trafilatura (article extract) / **SearXNG** (self-host, unlimited search, JSON API) / Playwright / Docling(MIT)/pymupdf4llm (PDF) / Patchright->Nodriver (anti-bot, lazy) / **FastMCP** Python, **Streamable-HTTP** transport. Deploy: systemd+uvicorn `127.0.0.1:8090` + nginx subdomain+TLS+bearer/JWT+fail2ban on VPS `103.172.172.29`; SearXNG docker `:8888`.

## MCP tools (20 live)
`read / search / smart_search(auto-route) / read_pdf / scrape / batch_read / extract_structured(selector+LLM) / crawl / screenshot / research(deep/quick/answer) / map_urls / find_similar(local semantic) / github_search / scholar_search / watch / list_watches / unwatch / forexfactory_calendar / cot_report / news_sentiment_feed`. Domain routing: `search(category=science->Scholar/arXiv, it->GitHub/SO, news, general)`; `smart_search` auto-picks via a deterministic classifier (no LLM). `read(extract_media)`=links+images; `research(highlights)`=top sentences. Per-host courtesy/circuit-breaker throttle; `/metrics` per-tool counters; JWT or static bearer auth.

## Working rules (MANDATORY - owner: full-autonomous, multi-agent, no questions)
- **Full autonomous, multi-agent for speed, NO questions to the user.** Brainstorming is DONE (see docs) -> go writing-plans -> TDD/build.
- **Skills/plugins/tools first** - invoke `superpowers` (writing-plans, test-driven-development, systematic-debugging, verification-before-completion), `python-development`, `backend-development`; use multi-agent (Agent tool / parallel) to fan out independent work.
- **Per component:** write -> syntax-check -> test -> validate, before moving on. No one giant turn - build incrementally with checks.
- **Commit + update status** (this file + docs/02-ROADMAP) after each milestone.
- **Honor the global SURIOTA CLAUDE.md** (Ponytail minimal-code, design-skill rules) + secret-handling (manage VPS secrets via SSH/scp directly, NOT via Hermes tools).

## Hard gates (never compromise)
- **SSRF resolve-then-validate** (deny private/metadata IPs, re-pin IP, anti-rebinding, scheme allowlist) - **100% test coverage**.
- **Trading-source parsers >=99% field accuracy** before any live Aurix use.
- **Benchmark:** formatting-invariant `quality_f1` ties/beats the best free baseline; success >=95%; no silent truncation/full-content gate holds; active v0.4.4 benchmark scope is non-trading by default.
- Security SAST + deps-audit before deploy; load test (no OOM/leak).

## VPS access
SSH key-only: `ssh -i C:\Users\Administrator\.ssh\gifari_vps_ed25519 ai@103.172.172.29` (Ubuntu 24.04). Coexists with Hermes :80 / SUVA :8080. Deployment is complete; remaining owner inputs are F1 `ARGUS_S2_API_KEY` and optional SearXNG `outgoing.proxies`. Leave `ARGUS_ENABLE_LLM` unset unless intentionally enabling the optional LLM tier. Deploy runbook: `deploy/README.md`.
