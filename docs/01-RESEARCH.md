# Argus - Research Findings (2026-06-24, 3-agent + 12-tool survey)

Condensed record. Full reasoning in git history / session. Verify pricing/licenses before relying - web data changes fast.

## A. Incumbent survey (12 tools, scored for our context)
Top by fit (remote-HTTP MCP + free tier + read/search): **Bright Data** (5k req/mo free, remote, best anti-bot) / **Firecrawl** (remote OAuth, full features) / **Exa** (semantic search) / **Claude built-in WebSearch/WebFetch** (zero setup/RAM, no JS/anti-bot, US-only search) / **Tavily** ((!) Nebius acquisition). Note 2026 changes: **Brave killed free tier**, Exa raised prices, Tavily acquired. **Conclusion that triggered Argus:** all meter/truncate/cost - self-hosting OSS removes all three.

## B. OSS stack (Agent A - verified GitHub licenses)
- **Reader core: Crawl4AI** - `unclecode/crawl4ai`, **69.4k*, Apache-2.0** (NOT AGPL - aggregators were wrong; verified on repo), v0.9.0 (2026-06-18), Python, bundles Playwright + ships FastAPI/Docker. **Clone & improve - the only fork.**
- **Article extraction: trafilatura** - best-in-class boilerplate removal (F1 ~0.937), Apache/GPL, static HTML. Use as extraction stage behind renderer.
- **Search: SearXNG** - 32.6k*, AGPL, self-host, 70+ engines, **no API key, JSON API** (`/search?format=json`). **The unlimited-search key.** Caveat: ~20 results/page (paginate); heavy volume can get upstream engines to rate-limit the VPS IP (mitigate w/ delay + engine rotation).
- **Render: Playwright** (Apache-2.0) - bundled in Crawl4AI; the 2026 default.
- **Anti-bot (lazy tier): Patchright** (stealth Playwright drop-in, Apache-2.0) -> **Nodriver** (2026 stealth benchmark winner, Apache-2.0) for hard targets.
- **PDF: Docling** (IBM, **MIT**, best tables) primary + **pymupdf4llm** (AGPL, fastest digital PDFs) fast path.
- **License posture:** AGPL (SearXNG, pymupdf) safe for **internal self-host** - copyleft only on distributing modified software / SaaS-to-third-parties. Calling AGPL over HTTP != derivative work. Core on Apache/MIT so a future *product* stays clean. **Avoid Firecrawl OSS as base** (AGPL + crippled self-host - anti-bot "Fire-Engine" is managed-only).

## C. MCP engineering + deploy (Agent B - official docs)
- **FastMCP** standalone (`fastmcp` pkg, v3.4.x, jlowin/PrefectHQ; != the SDK's bundled `mcp.server.fastmcp`). Python. `@mcp.tool` decorators. MCP donated to Linux Foundation Dec-2025 (vendor-neutral).
- **Streamable HTTP** transport = remote, **zero client process** (stdio is the only kind that spawns local). SSE deprecated. `mcp.http_app(path="/mcp")` under uvicorn. Client: `claude mcp add --transport http argus <url> --header "Authorization: Bearer ..."`; `.mcp.json` `{type:"http",url,headers}` with `${ENV}` expansion. Per-server `timeout` (ms) for slow scrapes; `MAX_MCP_OUTPUT_TOKENS` / `anthropic/maxResultSizeChars` (<=500k) to avoid truncating big scrapes. If header auth rejected, Claude Code reports failed (no OAuth fallback) - clean for static token.
- **Auth:** `StaticTokenVerifier` (dev/internal) -> `JWTVerifier` (prod, expiry/rotation). Plus nginx 2nd-layer + TLS + fail2ban. OAuth-2.1 mandate is for *public-internet* servers; bearer-behind-TLS defensible for locked-down internal.
- **VPS deploy:** bare systemd + uvicorn `127.0.0.1:8090` + nginx subdomain (`proxy_buffering off`!, `proxy_read_timeout 300s`) + TLS. SearXNG via official Docker `127.0.0.1:8888`. Coexist w/ Hermes :80 / SUVA :8080.
- **Chromium on headless Ubuntu 24.04 (top risk):** `crawl4ai-setup` + `crawl4ai-doctor`; install browser **as the `argus` service user** (cache in that user's `~/.cache/ms-playwright` - mismatched-user = #1 systemd failure); `playwright install --with-deps` needs root (do at provision, not service start); `--only-shell` to slim.
- **Concurrency:** single shared Chromium in lifespan + per-request context + `asyncio.Semaphore(4-8)`; uvicorn `--workers 1` (shared browser); `stateless_http=True` only if scaling out later.

## D. Feature matrix + benchmark + QA (Agent C)
- **MVP tools:** read, search, read_pdf, scrape, batch_read, extract_structured(selector). **P2:** crawl, screenshot, extract_structured(LLM), trading extractors, Docling. **P3:** watch, map, proxy rotation.
- **Edge/moat:** unlimited+free, zero truncation, custom ForexFactory/COT/news extractors (-> Aurix pipeline, replaces deprecated FMP), owned-LLM post-processing, content-addressed cache w/ per-source TTL, data sovereignty, composability.
- **Benchmark:** reference-based - 30 URLs/7 categories (news, SPA, docs, PDF, **trading sources**, anti-bot, long-form) + 10 queries, each w/ hand-curated gold extraction. Metrics: ROUGE-L/F1, success rate, truncation completeness, latency p50/p95, table fidelity, search nDCG@10, cost/1k. Uniform adapters; `run_bench.py` + scorer -> leaderboard + "where Argus loses".
- **QA hard gates:** SSRF resolve-then-validate (100% cov) / trading-parser field accuracy >=99% / success >=95% / truncation >=0.98 long-form / ROUGE-L >= best free competitor / load test (no OOM/leak) / security SAST + deps-audit.

## Key sources
Crawl4AI github.com/unclecode/crawl4ai / SearXNG github.com/searxng/searxng + docs.searxng.org/dev/search_api.html / FastMCP gofastmcp.com + pypi.org/project/fastmcp / Claude Code MCP code.claude.com/docs/en/mcp / trafilatura.readthedocs.io/evaluation / Docling github.com/docling-project/docling / Playwright stealth Patchright/Nodriver (ianlpaterson.com benchmark 2026) / OWASP SSRF cheat sheet / Crawl4AI install docs.crawl4ai.com/core/installation. (Full URL list in session transcript.)
