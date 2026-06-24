<div align="center">

<img src="assets/banner.svg" alt="Argus - Self-Hosted Web Intelligence MCP" width="100%" />

<br/>

<a href="https://github.com/jlowin/fastmcp"><img src="https://img.shields.io/badge/MCP-Streamable_HTTP-2dd4bf?style=for-the-badge&logo=anthropic&logoColor=white" alt="MCP"/></a>
<img src="https://img.shields.io/badge/tools-20-22c55e?style=for-the-badge" alt="20 tools"/>
<img src="https://img.shields.io/badge/tests-526_passing-3fb950?style=for-the-badge&logo=pytest&logoColor=white" alt="tests"/>
<img src="https://img.shields.io/badge/SSRF_coverage-100%25-16a34a?style=for-the-badge&logo=shieldsdotio&logoColor=white" alt="SSRF 100%"/>
<img src="https://img.shields.io/badge/python-3.12-3776AB?style=for-the-badge&logo=python&logoColor=white" alt="python"/>
<br/>
<img src="https://img.shields.io/badge/self--hosted-unlimited-0ea5e9?style=for-the-badge" alt="self-hosted"/>
<img src="https://img.shields.io/badge/cost-%240%2Frequest-22c55e?style=for-the-badge" alt="zero cost"/>
<img src="https://img.shields.io/badge/truncation-none-14b8a6?style=for-the-badge" alt="no truncation"/>
<img src="https://img.shields.io/badge/LLM-not_required-8957e5?style=for-the-badge" alt="no LLM needed"/>
<img src="https://img.shields.io/badge/owner-SURIOTA-0d9488?style=for-the-badge" alt="SURIOTA"/>

<br/><br/>

<!-- Dynamic repo-stat badges: render once the repo is PUBLIC (shields.io can't read private repos). -->
<img src="https://img.shields.io/github/last-commit/GifariKemal/argus-web-mcp?style=flat-square&color=22c55e" alt="last commit"/>
<img src="https://img.shields.io/github/languages/top/GifariKemal/argus-web-mcp?style=flat-square&color=2dd4bf" alt="top language"/>
<img src="https://img.shields.io/github/languages/count/GifariKemal/argus-web-mcp?style=flat-square&color=14b8a6" alt="languages"/>
<img src="https://img.shields.io/github/languages/code-size/GifariKemal/argus-web-mcp?style=flat-square&color=0ea5e9" alt="code size"/>
<img src="https://img.shields.io/github/repo-size/GifariKemal/argus-web-mcp?style=flat-square&color=8957e5" alt="repo size"/>
<img src="https://img.shields.io/github/commit-activity/m/GifariKemal/argus-web-mcp?style=flat-square&color=d29922" alt="commit activity"/>

<br/><br/>

<img src="https://readme-typing-svg.demolab.com/?font=Fira+Code&weight=600&size=22&pause=900&color=2DD4BF&center=true&vCenter=true&width=820&lines=The+all-seeing+web+layer+for+Claude+Code+%26+Codex;Replaces+Jina+%2F+Brave+%2F+Firecrawl+%2F+Exa+-+self-hosted;search+%2F+scrape+%2F+read+%2F+research+%2F+unlimited+%2F+owned;Tools%2C+not+a+brain+-+your+agent+does+the+thinking" alt="tagline"/>

</div>

---

> **Argus Panoptes** - the all-seeing hundred-eyed giant. A self-hosted, **unlimited**, **owned** web **fetch / scrape / search** MCP server for **SURIOTA**. Mythological sibling to the **Hermes AI Server**. Every Claude Code / Codex CLI connects over **remote HTTP** -> **zero local process** on the client.

## Why Argus

We surveyed the 12 leading paid/free web tools. **All** meter requests, truncate content, or cost money. Argus is built on best-in-class OSS, **self-hosted on the SURIOTA VPS** - so it's unlimited, free per-request, returns **full content (no truncation)**, and is **owned end-to-end**. It's **tools, not a brain**: the consuming agent (Claude Code's Opus 4.8 / Codex) does the reasoning - Argus needs **no LLM**.

| You were paying for... | Argus replaces it with... | Edge |
|---|---|---|
| Brave / Tavily / Exa **search** | `search` / `smart_search` (SearXNG, 70+ engines) | unlimited, multi-engine + **semantic rerank** |
| Jina Reader / Firecrawl **scrape** | `read` / `scrape` / `batch_read` / `crawl` | full content, JS render + stealth, **no truncation** |
| Jina / Firecrawl **PDF** | `read_pdf` (pymupdf4llm + Docling) | tables preserved (COT/FOMC) |
| Firecrawl **extract** / **map** | `extract_structured` / `map_urls` | CSS/XPath, sitemap discovery |
| Exa **findSimilar** / answer | `find_similar` / `research(deep)` | **local embeddings**, full-content bundle |
| - (no competitor self-hosts) | `github_search` / `scholar_search` / trading extractors / `watch` | structured GitHub / academic / FX moat |

## Architecture

<div align="center">
<img src="assets/architecture.svg" alt="Argus architecture" width="100%" />
</div>

**Fetch strategy (cheap -> expensive):** httpx static -> trafilatura -> escalate to Crawl4AI/Playwright only when JS/thin -> Patchright stealth on anti-bot -> **Wayback archive** if the host is unreachable. Every hop is SSRF-guarded.

## The 20 tools

<details open>
<summary><b>Search &amp; discovery</b></summary>

| Tool | What it does |
|---|---|
| `search` | Web search via SearXNG - categories (general/news/science/it), domain filters, safesearch, **hybrid lexical+semantic rerank**, recency boost, auto-backoff on throttle |
| `smart_search` | Auto-routes a query (deterministic, **no LLM**) -> github / scholar / news / it / general |
| `scholar_search` | Structured academic search (Semantic Scholar -> CrossRef): citations, DOI, abstract, OA-PDF |
| `github_search` | Structured GitHub `repositories`/`code`/`issues` + stars/language/sort |
| `map_urls` | Discover a site's URLs (sitemap.xml / robots.txt / 1-hop links) |
| `find_similar` | Semantically-related pages via **local embeddings** (Exa-style, no API) |

</details>

<details>
<summary><b>Fetch &amp; read</b></summary>

| Tool | What it does |
|---|---|
| `read` | URL -> clean markdown/text/html, **no truncation**; `extract_media` adds links+images |
| `scrape` | JS-rendered fetch + screenshot/actions; auto-escalates to **Patchright stealth** on anti-bot |
| `batch_read` | Parallel `read` over many URLs, partial-failure tolerant |
| `read_pdf` | PDF -> markdown + tables (pymupdf4llm; `mode='quality'` -> Docling for scanned/complex) |
| `crawl` | Crawl4AI BFS deep-crawl, robots-respecting, domain-confined |

</details>

<details>
<summary><b>Extract &amp; research</b></summary>

| Tool | What it does |
|---|---|
| `extract_structured` | Pull fields via CSS/XPath selectors (deterministic); optional LLM tier (`auto`/`llm`) |
| `research` | One-shot: `deep` (search->full-read top-K->bundle) / `quick` (hits) / `answer` (cited, opt-in LLM); `highlights`=top sentences per source |

</details>

<details>
<summary><b>Monitoring &amp; trading moat</b></summary>

| Tool | What it does |
|---|---|
| `watch` / `list_watches` / `unwatch` | Poll a page (full or selector) -> POST to a webhook (e.g. Telegram) on change |
| `forexfactory_calendar` | Economic calendar (FairEconomy JSON) -> Aurix `calendar_client` shape |
| `cot_report` | CFTC Commitments of Traders, structured |
| `news_sentiment_feed` | Ranked news + optional sentiment score |

> Trading parsers are validated to **>=99% field accuracy** (100% on golden files).

</details>

## Hard guarantees

- **SSRF** - resolve-then-validate, IP-pin anti-rebinding, scheme allowlist, per-hop redirect re-check, private/metadata/CGNAT deny -> **100% test coverage** (hard gate).
- **No silent truncation** - full documents always; the streaming body cap is a DoS guard, not a content cap.
- **Resilience** - content-addressed cache (per-source TTL, stale-serve), per-host courtesy delay + circuit breaker, archive egress-fallback.
- **Secure deploy** - bearer/JWT auth + nginx TLS + fail2ban; runs unprivileged via systemd; secrets via `EnvironmentFile`.

## Quickstart (local)

```bash
uv venv --python 3.12 && uv pip install -e ".[dev]"
crawl4ai-setup && crawl4ai-doctor          # one-time Chromium
# optional extras: ".[semantic]" (find_similar/rerank), ".[pdf-quality]" (Docling)

# SearXNG (search backend) - loopback only
cd deploy/searxng && docker compose up -d

python -m argus.server                      # stdio (local dev)
# or HTTP:  uvicorn argus.server:app --host 127.0.0.1 --port 8090
```

Register with **Claude Code**:
```bash
claude mcp add --transport http argus https://argus.<domain>/mcp \
  --header "Authorization: Bearer ${ARGUS_TOKEN}"
```
Register with **Codex** (`~/.codex/config.toml`):
```toml
[mcp_servers.argus]
url = "https://argus.<domain>/mcp"
bearer_token_env_var = "ARGUS_TOKEN"
```

## Benchmarked

Head-to-head vs Claude Code & Codex **native** web tools (n=50, identical queries): **discovery parity**, but Argus wins decisively on **content depth** (~7,000 words of full content per query vs hits/summaries), freshness, and cost/ownership. Semantic rerank quantified at **+27% nDCG** on conceptual queries. Full report: [`benchmark/RESULTS.md`](benchmark/RESULTS.md).

## Repo map

| Path | What |
|---|---|
| `src/argus/` | the package - `server.py` (20 tools), `fetch/`, `extract/`, `security/ssrf.py`, `trading/`, `semantic.py`, `cache.py`, `watch.py` |
| `docs/` | [DESIGN](docs/00-DESIGN.md) / [ROADMAP](docs/02-ROADMAP.md) / [TOOL-SPECS](docs/03-TOOL-SPECS.md) / [COMPETITIVE-GAP](docs/05-COMPETITIVE-GAP.md) |
| `benchmark/` | harness + [RESULTS](benchmark/RESULTS.md) + head-to-head |
| `deploy/` | systemd / nginx / provision.sh / fail2ban / [SECURITY-AUDIT](deploy/SECURITY-AUDIT.md) / searxng/ |
| [`SOUL.md`](SOUL.md) / [`AGENTS.md`](AGENTS.md) / [`CHANGELOG.md`](CHANGELOG.md) | identity / agent guide / history |

## Status

**Feature-complete & validated locally.** 20 tools / 526 offline tests (+3 browser, +2 slow) green / SSRF 100% / ruff clean / security-audited (no Critical/High). **Deploy to the VPS is gated only on owner inputs** (subdomain + DNS, `ARGUS_TOKEN`, optional SearXNG proxy) - see [`docs/02-ROADMAP.md`](docs/02-ROADMAP.md).

<div align="center">
<sub>Built for <b>PT Surya Inovasi Prioritas (SURIOTA)</b> / self-hosted / unlimited / owned</sub>
</div>
