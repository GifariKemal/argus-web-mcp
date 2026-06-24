# Argus - Design Document

_Status: design complete (autonomous brainstorm, 2026-06-24). Self-directed per owner's "full autonomous, no questions" directive - decisions documented here in lieu of an interactive approval gate._

## 1. Goal & constraints

Build a self-hosted MCP server (`Argus`) exposing web **search / read / scrape / pdf / extract** as MCP tools, deployed on the SURIOTA VPS (Ubuntu 24.04, `103.172.172.29`, 52 GB - shares the box with Hermes :80 + SUVA :8080). Every Claude Code CLI connects **remotely over HTTP** -> no local process on the client.

**Hard requirements:**
1. Remote HTTP transport (zero client-side process).
2. Unlimited / no per-request cost (self-hosted).
3. Match-or-beat the 12 surveyed tools on quality; win on cost + truncation + trading-source extraction.
4. Secure (SSRF-hardened), reliable (browser-crash recovery), observable.

## 2. Architecture (layers)

```
Claude Code CLI  --HTTPS (Bearer)-->  nginx (argus.<domain>, TLS, fail2ban)
                                         |  proxy_buffering off
                                         v
                              uvicorn  127.0.0.1:8090
                                         |
                                   FastMCP app  (Streamable HTTP /mcp + /health + /metrics)
                                         |
                    +--------------------+-------------------------+
                 MCP tools          shared services            cache (content-addressed)
              read/search/scrape    - browser pool (1 Chromium,   SQLite+disk, per-source TTL
              read_pdf/batch_read     per-req context, semaphore)
              extract_structured    - httpx pool (static fast-path)
                                     - SearXNG client (search)
                                     - owned-LLM client (extract/summarize)
                    |
        +-----------+-----------+--------------+---------------+
   Crawl4AI     trafilatura   SearXNG svc    Docling/        Patchright/Nodriver
   (render+md)  (article)     (Docker,       pymupdf4llm     (stealth, lazy tier)
                              :8888 local)   (PDF)
```

**Fetch strategy (cheap -> expensive):** httpx static GET -> trafilatura extract. If JS needed / thin content -> Crawl4AI+Playwright. If anti-bot block -> Patchright -> Nodriver. This minimizes browser cost (the expensive path).

## 3. MCP tool surface

| Tool | Phase | Backing | Notes |
|---|---|---|---|
| `read(url, format, clean)` | P1 | trafilatura -> readability fallback -> Crawl4AI | clean markdown + metadata, **no truncation** |
| `search(query, category, count, time_range)` | P1 | SearXNG JSON API | unlimited, 70+ engines |
| `read_pdf(url/path, pages, mode)` | P1 | pymupdf4llm fast / Docling quality | tables preserved (COT/FOMC) |
| `scrape(url, wait_for, actions, screenshot)` | P1 | Crawl4AI + Playwright | JS render |
| `batch_read(urls[], concurrency)` | P1 | asyncio + httpx pool | partial-failure tolerant |
| `extract_structured(url, schema)` | P1 sel / P2 LLM | parsel (CSS/XPath) -> Instructor+Pydantic+owned LLM | deterministic first, LLM only if needed |
| `crawl(seed, depth, max_pages, globs)` | P2 | Crawl4AI deep-crawl | robots-respecting |
| `screenshot(url, full_page)` | P2 | Playwright | |
| `watch(url, selector, interval, webhook)` | P3 | Playwright + diff + APScheduler | calendar/COT monitoring -> Telegram |
| `map(url)` | P3 | sitemap + link extraction | |

**Trading-specialized** (behind `read`/`extract_structured`, the real moat): `forexfactory_calendar`, `cot_report`, `news_sentiment_feed` -> JSON keyed to Aurix `calendar_client`/fundamentals.

## 4. MCP framework & transport
- **FastMCP** (standalone `fastmcp` pkg, v3.x; floor 2.11+ for auth), Python 3.11. Tools via `@mcp.tool`. Keep server `instructions` < 2 KB (Tool Search uses them).
- **Streamable HTTP** transport (`mcp.http_app(path="/mcp")` under uvicorn). SSE deprecated - not used.
- Client adds: `claude mcp add --transport http argus https://argus.<domain>/mcp --header "Authorization: Bearer ${ARGUS_TOKEN}"`. `.mcp.json` entry = `{type:"http", url, headers}` -> **no local process**.
- Per-server `timeout` high (slow scrapes); raise `MAX_MCP_OUTPUT_TOKENS` / annotate `anthropic/maxResultSizeChars` (<=500k) so big scrapes aren't truncated.

## 5. Concurrency
- **Single shared Chromium** launched in FastMCP lifespan; **per-request browser context** (cheap) - never browser-per-request.
- `asyncio.Semaphore` (4-8, RAM-sized) caps concurrent pages; per-request `goto` timeout + overall tool timeout.
- uvicorn `--workers 1` (shared stateful browser). Horizontal scale later via `stateless_http=True`.

## 6. Security (hard gates)
- **SSRF: resolve-then-validate** - resolve DNS, deny if IP  in  private ranges (127/8,10/8,172.16/12,192.168/16,169.254/16) or metadata (169.254.169.254 + IPv6 fd00:ec2::254); **re-pin resolved IP** for the connection (anti DNS-rebinding); re-validate each redirect hop. 100% test coverage on this path.
- Scheme allowlist `http`/`https` only.
- Auth: bearer token (`StaticTokenVerifier` v1 -> `JWTVerifier` prod) + nginx second-layer auth/IP-allowlist + TLS + fail2ban jail. Secret via `EnvironmentFile`, managed by SSH/scp directly (NOT via Hermes tools - `redact_secrets` masks).
- robots.txt + per-domain courtesy delay by default.
- No secrets in logs (reuse Hermes `redact_secrets` discipline).

## 7. Reliability
- Per-tool connect+total timeouts; retry w/ backoff+jitter; per-host circuit breaker (pattern from Aurix `llm_advisor`).
- Browser-crash recovery: context pool health-check + auto-respawn; OOM guard via semaphore.
- Partial-failure tolerance (`batch_read`/`crawl` return successes + error list).
- Cache fallback: store-good-only + stale-serve on transient failure (pattern from Aurix DEXT D1 cache fix).

## 8. Observability
- `/health` (browser liveness) - Hermes watchdog cron curls it. `/metrics` Prometheus (requests/errors per tool, latency histogram, **active-context gauge** = OOM early-warning). Structured per-tool logs -> journald.

## 9. Deploy topology (VPS)
- Bare systemd (matches Hermes/SUVA), not Docker (avoids Chromium-in-container pain) - except SearXNG runs as its official Docker image on `127.0.0.1:8888`.
- `argus.service`: `uvicorn argus.server:app --host 127.0.0.1 --port 8090`, unprivileged `User=argus`, `EnvironmentFile` secret, `Restart=on-failure`, `--workers 1`.
- nginx `argus.<domain>` -> `127.0.0.1:8090`, **`proxy_buffering off`**, `proxy_read_timeout 300s`, TLS (certbot), fail2ban.
- Playwright/Chromium installed **once as the `argus` user** (`crawl4ai-setup` + `crawl4ai-doctor`); browser cache under `argus`'s `~/.cache/ms-playwright` (mismatched-user cache = #1 systemd failure).
- Ports: SearXNG 8888, Argus 8090 (avoid Hermes :80 / SUVA :8080).

## 10. License posture
AGPL (SearXNG, pymupdf) is **safe for internal self-hosting** - copyleft triggers only on distributing modified software / SaaS-to-third-parties. Core built on **Crawl4AI Apache-2.0** + **Docling MIT** so that IF Argus ever becomes a distributed product, the load-bearing pieces stay permissive (swap pymupdf->Docling, drop SearXNG forks). We call AGPL services over HTTP (not a derivative work).

## 11. Out of scope (YAGNI for now)
Distributed multi-node crawling, a real owned search index (YaCy), paid residential proxy pool, headed-browser mode, multi-tenant billing. Revisit only if a concrete need appears.
