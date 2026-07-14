# Argus - OSS Study Guide & References

What to clone/study from each project, the key modules/APIs, and the URLs.

> [!TIP]
> Read these (fetch the docs/repos) before/while building each component so we adopt proven patterns, not reinvent.

## Core - CLONE & improve
### Crawl4AI (Apache-2.0) - the reader/render core
- Repo: https://github.com/unclecode/crawl4ai / Docs: https://docs.crawl4ai.com
- **Study:** `AsyncWebCrawler` (lifecycle), `BrowserConfig`/`CrawlerRunConfig`, **markdown generators** (`DefaultMarkdownGenerator`, "fit markdown" boilerplate removal - `docs.crawl4ai.com/core/markdown-generation`), browser-manager (single browser + per-context pattern), its FastAPI/Docker server + the community FastMCP server (shape to mirror), deep-crawl strategy (for `crawl`).
- **Adopt:** lifespan-managed shared Chromium + per-request context; the tiered markdown pipeline. **Improve:** our caching, SSRF guard, MCP tool shape, trading extractors.
- Install: `pip install crawl4ai` -> `crawl4ai-setup` -> `crawl4ai-doctor` (Python 3.10+). v0.9.0 (2026-06).

## Search - self-host as dependency
### SearXNG (AGPL - safe internal) - unlimited search backend
- Repo: https://github.com/searxng/searxng / **Search API: https://docs.searxng.org/dev/search_api.html**
- **Study:** enable JSON in `settings.yml`, `GET /search?q=...&format=json&categories=...&time_range=...&pageno=N` -> `{results:[{title,url,content,engine}]}`. Run official Docker image; set `secret_key`; bind `127.0.0.1:8888` behind our app.
- **Caveats:** ~20 results/page (paginate via `pageno`); heavy volume -> upstream engines may rate-limit the VPS IP -> add per-engine delay + rotation; optionally proxy.

## Extraction
### trafilatura (Apache/GPL) - best article main-content extraction (F1 ~0.937)
- Repo: https://github.com/adbar/trafilatura / Eval: https://trafilatura.readthedocs.io/en/latest/evaluation.html
- **Study:** `extract()` (output_format md/json/xml, include_links, with_metadata), favor-precision vs recall knobs. Static HTML only -> run behind renderer for JS pages.
- Fallbacks: readability-lxml (Mozilla algorithm), markdownify/turndown (HTML->md).

### PDF - Docling (MIT) + pymupdf4llm (AGPL)
- Docling: https://github.com/docling-project/docling (best tables/multi-column/scanned; loads ML models, slower cold). pymupdf4llm: https://pymupdf.readthedocs.io/en/latest/pymupdf4llm/ (fastest digital PDF -> markdown).
- **Pattern:** pymupdf4llm fast-path -> Docling for table/scanned quality.

### Structured - Instructor + Pydantic (over owned LLM)
- Instructor: https://github.com/567-labs/instructor (schema-validated LLM extraction). parsel (CSS/XPath) for the deterministic selector tier first; LLM only when selectors insufficient.

## Anti-bot (lazy tier - add only when blocked)
- Patchright (stealth Playwright drop-in, Apache-2.0): https://github.com/Kaliiiiiiiiii-Vinyzu/patchright - same Playwright API, swap in for protected sites.
- Nodriver (Apache-2.0, 2026 stealth benchmark winner): https://github.com/ultrafunkamsterdam/nodriver - escalate for hardest targets.

## MCP framework & deploy
### FastMCP (Python) - the server
- Docs: https://gofastmcp.com / Repo: https://github.com/jlowin/fastmcp / PyPI `fastmcp` (v3.x; >=2.11 for auth).
- **Study:** `FastMCP(name, instructions)`, `@mcp.tool`/`@mcp.resource`, **HTTP deployment** (`mcp.http_app(path="/mcp")` - https://gofastmcp.com/deployment/http), **lifespan** (init shared browser; MUST pass `lifespan=` if wrapping in Starlette/FastAPI), **token auth** (`StaticTokenVerifier`->`JWTVerifier`, https://gofastmcp.com/servers/auth/token-verification), `stateless_http` for scale.
- Keep `instructions` < 2 KB (Claude Code Tool Search uses it to decide when to surface tools).

### Claude Code MCP client
- Docs: https://code.claude.com/docs/en/mcp - remote HTTP add (`claude mcp add --transport http`), `.mcp.json` `{type:"http",url,headers}` with `${ENV}` expansion, per-server `timeout`, `MAX_MCP_OUTPUT_TOKENS`. **Streamable HTTP = zero local process** (stdio is the only kind that spawns one). SSE deprecated.

### Deploy patterns
- systemd + uvicorn + nginx (`proxy_buffering off`, `proxy_read_timeout 300s`) + TLS (certbot) + fail2ban. Playwright on Ubuntu 24.04: `playwright install --with-deps chromium` once as root at provision; browser cache under the **service user's** `~/.cache/ms-playwright`.

## Security
- OWASP SSRF Prevention Cheat Sheet: https://cheatsheetseries.owasp.org/cheatsheets/Server_Side_Request_Forgery_Prevention_Cheat_Sheet.html - resolve-then-validate, private/metadata IP deny, anti-DNS-rebinding, scheme allowlist.

## Benchmark
- Reference-based scoring (ROUGE-L/F1 vs gold). Web-extraction benchmark background: WebMainBench / trafilatura eval. Our set: `benchmark/testset.yaml`.
