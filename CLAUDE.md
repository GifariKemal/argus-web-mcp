# CLAUDE.md - Argus Web MCP

> Auto-loaded by Claude CLI in this directory. Argus = SURIOTA's **self-hosted, unlimited, owned** web **fetch / scrape / search** MCP server. *Created 2026-06-24.*

## What this is & scope
A FastMCP (Python) server, deployed remote-HTTP on the SURIOTA VPS, that every Claude Code CLI connects to over HTTP (**zero local client process**). Replaces paid tools (Jina/Firecrawl/Exa/Tavily/Bright Data) with self-hosted OSS -> unlimited, free, full-content, owned.

**General-purpose (owner directive):** Argus serves **all SURIOTA domains** - firmware/ESP32 (ESP-IDF docs), trading/Aurix (ForexFactory/COT/macro), web/Flutter/Node, business/market research, AI servers - **and broad general web research** (the "12 use cases": search, read, scrape, research, news, docs, PDF). Trading extractors are a *specialized moat*, not the only purpose. Don't narrow it to trading.

## Start here (read before coding)
0. [`AGENTS.md`](AGENTS.md) - how to work here (setup, verified command table, hard gates, conventions). [`SOUL.md`](SOUL.md) - identity/principles. [`README.md`](README.md) - showcase. [`CHANGELOG.md`](CHANGELOG.md) - history.
1. `docs/00-DESIGN.md` - architecture, stack, MCP tools, deploy, security.
2. `docs/02-ROADMAP.md` - step-by-step phases P0->P4 + exit-gate per phase. **Execute in order.**
3. `docs/03-TOOL-SPECS.md` - MCP tool I/O contracts.
4. `docs/04-REFERENCES.md` - OSS study guide (Crawl4AI/SearXNG/FastMCP/trafilatura - what to learn + URLs).
5. `docs/01-RESEARCH.md` - research findings + sources. `benchmark/testset.yaml` - benchmark set.

## Status
Live on the SURIOTA primary server as a Coolify service (`argus` + `searxng` + `warp`), public endpoint `https://argus.gifariksuryo.xyz/mcp` (bearer). Version 0.4.39 (`pyproject.toml`). Easypanel on the old VPS hosted it until 2026-10-30 and no longer runs it. Current snapshot, benchmarks, search-engine notes and open owner inputs: [`docs/STATUS.md`](docs/STATUS.md). History per version: [`CHANGELOG.md`](CHANGELOG.md). Plans in `docs/plans/`, QA reports in `docs/qa/`.

## Quick start
Full table in [`AGENTS.md`](AGENTS.md#commands). Windows: call the venv binaries explicitly.

```bash
uv venv --python 3.12 && uv pip install -e ".[dev]"
./.venv/Scripts/python.exe -m pytest -q -m 'not browser and not slow and not network'   # offline suite
./.venv/Scripts/python.exe -m pytest tests/test_ssrf.py --cov=argus.security.ssrf --cov-branch --cov-fail-under=100 -q
./.venv/Scripts/ruff.exe check src tests benchmark
docker compose -f docker-compose.yml -f docker-compose.local.yml up -d   # local stack, MCP on http://127.0.0.1:8090/mcp, no auth
```

Other entrypoints: `python -m argus.server` (stdio), `uvicorn argus.server:app` (HTTP). The host port lives in the local overlay only: the base compose file pins no `container_name` and publishes no port, because Easypanel flags both as conflict risks.

## Architecture
**Stack (decided):** Crawl4AI (Apache-2.0, clone&improve core) / trafilatura (article extract) / **SearXNG** (self-host, unlimited search, JSON API) / Playwright / Docling(MIT)/pymupdf4llm (PDF) / playwright-stealth tier (anti-bot, lazy; Patchright measured in 0.4.21 and not adopted) / **FastMCP** Python, **Streamable-HTTP** transport / Cloudflare **WARP** container as egress proxy for `ARGUS_EGRESS_PROXY_HOSTS`. Deploy: **Coolify** (Docker + Traefik) on the SURIOTA primary server runs host-built images of a pinned ref (Easypanel until 2026-10-30); **Cloudflare proxies the DNS record** and Traefik accepts the `/` router only from Cloudflare ranges, terminates TLS, and Argus itself enforces the bearer/JWT. `deploy/provision.sh` (systemd + nginx + certbot + fail2ban) stays as the panel-less alternative.

**MCP tools (21 live):** `read / search / smart_search(auto-route) / read_pdf / scrape / batch_read / extract_structured(selector+LLM) / crawl / screenshot / research(deep/quick/answer) / map_urls / find_similar(local semantic) / github_search / scholar_search / x_search(X posts: live via one cookie account, index fallback) / watch / list_watches / unwatch / forexfactory_calendar / cot_report / news_sentiment_feed`. Domain routing: `search(category=science->Scholar/arXiv, it->GitHub/SO, news, general)`; `smart_search` auto-picks via a deterministic classifier (no LLM). `read(extract_media)`=links+images; `research(highlights)`=top sentences. Per-host courtesy/circuit-breaker throttle; `/metrics` per-tool counters; JWT or static bearer auth. Code layout: [`AGENTS.md`](AGENTS.md#layout--conventions).

**No LLM needed:** Argus is tools-not-brain - the consuming agent (Claude Code / Codex) does synthesis from `research(deep)`/`quick` raw-content bundles. The LLM tier (research `answer`, extract_structured `llm`) is optional and **off by default** (requires `ARGUS_ENABLE_LLM=1` + endpoint). Leave `ARGUS_ENABLE_LLM` unset unless intentionally enabling it.

## Working rules
- Work autonomously inside the repo: plans, TDD, multi-agent fan-out for independent modules. Brainstorming is done (see docs) -> writing-plans -> TDD/build.
- One branch: `main`. A push to `main` no longer deploys (no webhook since 2026-10-30); production is built and rolled out by hand on the primary server (Coolify). Commit locally per milestone, **push only when the user asked**, and ask before server or panel commands.
- **Skills/plugins/tools first** - `superpowers` (writing-plans, test-driven-development, systematic-debugging, verification-before-completion), `python-development`, `backend-development`.
- **Per component:** write -> syntax-check -> test -> validate, before moving on. No one giant turn.
- After each milestone update `docs/STATUS.md`, `CHANGELOG.md` and `docs/02-ROADMAP.md`.
- Honor the global SURIOTA CLAUDE.md (Ponytail minimal-code). Manage VPS secrets via SSH/scp directly; never commit them.

## Hard gates (never compromise)
- **SSRF resolve-then-validate** (deny private/metadata IPs, re-pin IP, anti-rebinding, scheme allowlist) - **100% test coverage**.
- **Trading-source parsers >=99% field accuracy** before any live Aurix use.
- **Benchmark:** formatting-invariant `quality_f1` ties/beats the best free baseline; success >=95%; no silent truncation/full-content gate holds; active v0.4.4 benchmark scope is non-trading by default.
- Security SAST + deps-audit before deploy; load test (no OOM/leak).

## VPS access
Not repeated in this public repo. Local details: `CLAUDE.local.md` (gitignored, owner machine only). Deploy runbook: `deploy/README.md`. Owner inputs: `ARGUS_S2_API_KEY` (set in the panel env, never in the repo) and optional SearXNG `outgoing.proxies`.

## Do not
- Push to `main` without the user asking (it ships to production).
- Commit secrets; put VPS addresses or SSH details in agent docs (use `CLAUDE.local.md`).
- Add a paid web API to "fix" search, or make Argus depend on an LLM.
- Weaken a hard gate "temporarily".
