# Argus - Design Document

> [!NOTE]
> Status: design complete (autonomous brainstorm, 2026-06-24); **fully built and DEPLOYED LIVE** 2026-06-25 at `https://argus.gifariksuryo.xyz/mcp` - 20 MCP tools. This doc captures the original design; the shipped tool surface grew past the P1 table below - see `docs/03-TOOL-SPECS.md` and `CHANGELOG.md` for the live 20-tool set. Self-directed per owner's "full autonomous, no questions" directive - decisions documented here in lieu of an interactive approval gate.

## Contents

- [1. Goal & constraints](#1-goal--constraints)
- [2. Architecture (layers)](#2-architecture-layers)
- [3. MCP tool surface](#3-mcp-tool-surface)
- [4. MCP framework & transport](#4-mcp-framework--transport)
- [5. Concurrency](#5-concurrency)
- [6. Security (hard gates)](#6-security-hard-gates)
- [7. Reliability](#7-reliability)
- [8. Observability](#8-observability)
- [9. Deploy topology (VPS)](#9-deploy-topology-vps)
- [10. License posture](#10-license-posture)
- [11. Out of scope (YAGNI for now)](#11-out-of-scope-yagni-for-now)

## 1. Goal & constraints

Build a self-hosted MCP server (`Argus`) exposing web **search / read / scrape / pdf / extract** as MCP tools, deployed on the SURIOTA VPS (Ubuntu 24.04, `43.134.17.144`, 2 vCPU / 7 GB / 99 GB disk; the previous box `103.172.172.29`, shared with Hermes :80 + SUVA :8080, died on 2026-09-15). Every Claude Code CLI connects **remotely over HTTP** -> no local process on the client.

**Hard requirements:**
1. Remote HTTP transport (zero client-side process).
2. Unlimited / no per-request cost (self-hosted).
3. Match-or-beat the 12 surveyed tools on quality; win on cost + truncation + trading-source extraction.
4. Secure (SSRF-hardened), reliable (browser-crash recovery), observable.

## 2. Architecture (layers)

<p align="center">
  <img src="../assets/architecture.svg" alt="Argus architecture: Claude Code / Codex CLI over HTTPS to nginx, uvicorn+FastMCP, 20 MCP tools with shared services and SSRF guard, backed by SearXNG / Crawl4AI / trafilatura / Docling" width="100%">
</p>

Request path: **Claude Code / Codex CLI** connect over HTTPS (bearer/JWT) to **Cloudflare**, which proxies to **Traefik** on the VPS; Traefik terminates TLS and forwards `/mcp` to **uvicorn** on the `argus` container's `:8090`, running the **FastMCP** app (Streamable HTTP `/mcp` + `/health` + `/metrics`). The app fans out to the **20 MCP tools**, **shared services** (browser pool, httpx, semantic embeddings, cache, throttle), and the **SSRF guard**, which reach the OSS backends: **SearXNG** (`http://searxng:8080` on the compose network), **Crawl4AI/Playwright**, **trafilatura/Docling**, and structured/fallback APIs (archive.org, GitHub, Semantic Scholar). Cache is content-addressed (SQLite + disk, per-source TTL). The diagram above still draws the retired nginx/systemd edge; section 9 is authoritative.

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
| `screenshot(url)` | P2 | Playwright | always full-page |
| `watch(url, selector, interval, webhook)` | P3 | Playwright + diff + APScheduler | calendar/COT monitoring -> Telegram |
| `map(url)` | P3 | sitemap + link extraction | |

**Trading-specialized** (behind `read`/`extract_structured`, the real moat): `forexfactory_calendar`, `cot_report`, `news_sentiment_feed` -> JSON keyed to Aurix `calendar_client`/fundamentals.

## 4. MCP framework & transport
- **FastMCP** (standalone `fastmcp` pkg, v3.x; floor 2.11+ for auth), Python 3.11. Tools via `@mcp.tool`. Keep server `instructions` < 2 KB (Tool Search uses them).
- **Streamable HTTP** transport (`mcp.http_app(path="/mcp")` under uvicorn). SSE deprecated - not used.
- Client adds: `claude mcp add --transport http argus https://argus.gifariksuryo.xyz/mcp --header "Authorization: Bearer ${ARGUS_TOKEN}"`. `.mcp.json` entry = `{type:"http", url, headers}` -> **no local process**.
- Per-server `timeout` high (slow scrapes); raise `MAX_MCP_OUTPUT_TOKENS` / annotate `anthropic/maxResultSizeChars` (<=500k) so big scrapes aren't truncated.

## 5. Concurrency
- **Single shared Chromium** launched in FastMCP lifespan; **per-request browser context** (cheap) - never browser-per-request.
- `asyncio.Semaphore` (4-8, RAM-sized) caps concurrent pages; per-request `goto` timeout + overall tool timeout.
- uvicorn `--workers 1` (shared stateful browser). Horizontal scale later via `stateless_http=True`.

## 6. Security (hard gates)

> [!WARNING]
> SSRF resolve-then-validate is a hard gate with 100% test coverage. Do not weaken the private/metadata IP deny, the IP re-pin, or the per-redirect re-check.

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

Current, since the 2026-09-15 move to `43.134.17.144`:

- **Easypanel Compose service** built from the repo's own `docker-compose.yml`: an `argus`
  container (uvicorn `:8090`, `--workers 1`) and a `searxng` one, talking over the compose
  network at `http://searxng:8080`. No host port is published for either; Compose rather
  than an Easypanel App service because Chromium needs `shm_size: 1gb`, which Swarm cannot set.
- **Cloudflare** proxies `argus.gifariksuryo.xyz`, then **Traefik** (owned by Easypanel)
  terminates Let's Encrypt TLS on `:80`/`:443`. The `/` router carries `argus-cloudflare-only`
  (ipAllowList over the published Cloudflare ranges), so the origin cannot be reached around
  the edge, plus `argus-ratelimit` (300/60 s) keyed on `CF-Connecting-IP`. `/metrics` is a
  separate, longer-matching router gated to loopback and the docker ranges.
- Middlewares live in Easypanel's database and `traefik/config/main.yaml` is generated from
  it, so they are edited through the panel API, never in that file.
- A GitHub push webhook redeploys `main`. Chromium lives in the image, so the old
  mismatched-Playwright-cache failure mode is gone.

Retired but kept on disk as the panel-less recipe (`deploy/provision.sh`): bare systemd
`argus.service`, nginx vhost with `proxy_buffering off` + certbot + fail2ban, and SearXNG as
a standalone container on `127.0.0.1:8888`.

## 10. License posture

<details>
<summary>AGPL vs permissive split and the reasoning</summary>

AGPL (SearXNG, pymupdf) is **safe for internal self-hosting** - copyleft triggers only on distributing modified software / SaaS-to-third-parties. Core built on **Crawl4AI Apache-2.0** + **Docling MIT** so that IF Argus ever becomes a distributed product, the load-bearing pieces stay permissive (swap pymupdf->Docling, drop SearXNG forks). We call AGPL services over HTTP (not a derivative work).

</details>

## 11. Out of scope (YAGNI for now)
Distributed multi-node crawling, a real owned search index (YaCy), paid residential proxy pool, headed-browser mode, multi-tenant billing. Revisit only if a concrete need appears.
