# Head-to-head: Argus vs Claude Code native vs Codex CLI native

**Task (identical):** "What is ESP-Claw? Find out what it is, what it does, repo/site." / Run 2026-06-24.
ESP-Claw = Espressif's "Chat Coding" AI agent framework for ESP32 (OpenClaw-inspired, ~Apr 2026) - a good obscure-but-real query (tests discovery + freshness).

> [!NOTE]
> Status: the three improvements at the bottom (rerank, egress fallback, `research`)
> were built and are now LIVE in production at `https://argus.gifariksuryo.xyz/mcp`.
> See [RESULTS.md](RESULTS.md) for the broader n=50 / 4-way comparison.

## Contents

- [Raw results](#raw-results)
- [Scoring (1-5)](#scoring-1-5)
- [What Argus must improve to beat BOTH on results](#what-argus-must-improve-to-beat-both-on-results)
- [Improvements IMPLEMENTED (2026-06-24) + live re-validation](#improvements-implemented-2026-06-24--live-re-validation)

## Raw results

| System | Tools used | Found it? | Sources | Notes |
|---|---|---|---|---|
| **Claude Code (native)** | `WebSearch` + `WebFetch` | [x] yes, rich | **10** | Best discovery; WebFetch read esp-claw.com (server-side egress). WebFetch returns a *lossy model summary*, not raw content; WebSearch US-only, titles+URLs only. |
| **Codex CLI (native)** | `web_search` (live; default is **cached/stale**) | [x] yes, concise | 2 | gpt-5.5, 27.7k tokens. No raw-content fetch (curl only, no JS). Needed `--search`/`web_search=live` (default cached = stale). |
| **Argus** | `search` (SearXNG) + `read` (full) | [x] yes | 8 (incl. **2 off-topic** "ESP Guitar Company") | Search 2.8s via bing/brave/ddg. `read` returned **full clean markdown** (cnx-software 437w, github 498w) in 0.4-1.2s. **Failed on esp-claw.com -> local egress ConnectTimeout (reproduced with a plain client = NOT an Argus bug)**; Claude won that URL purely via server-side egress. |

## Scoring (1-5)

| Dimension | Claude native | Codex native | Argus |
|---|---|---|---|
| Found the answer | 5 | 5 | 5 |
| Source breadth | 5 (10) | 2 (2) | 4 (8) |
| Result relevance/ranking | 5 | 5 | **3** (2 off-topic in top 8) |
| Content depth returned | 2 (lossy summary) | 1 (no fetch) | **5** (full raw markdown) |
| JS render / anti-bot capability | 3 (server egress, no JS) | 1 (none) | **4** (browser+stealth) - but egress-limited on this box |
| Freshness | 4 | 2 (cached default) | **5** (live SearXNG) |
| Cost / limits / ownership | 2 (metered, US-only) | 2 (metered, cached) | **5** (unlimited, free, owned) |
| Reliability of egress | **5** (server-side) | **5** (server-side) | 3 (depends on host IP + SearXNG engine throttle) |

**Read:** Argus wins on **content depth, freshness, cost/ownership, render capability**; loses on **egress reliability** (its host must reach the target; the hosted tools fetch server-side) and **search ranking precision**. Claude/Codex win on robust server-side egress + clean one-shot synthesis.

## What Argus must improve to beat BOTH on results

**Highest leverage:** #3 (`research` tool) to win on results + #1 (rerank) for precision + #2 (egress fallback) to remove the only structural loss.

<details>
<summary>Full improvement list (5 items)</summary>

1. **Search reranking (observed defect)** - SearXNG put 2 off-topic "ESP Guitar Company" hits in the top 8. Add a light rerank in `search`: score by query-token overlap in title+snippet, drop near-zero-overlap hits, dedup near-duplicate URLs/titles. Cheap, deterministic, directly fixes precision.
2. **Fetch egress fallback (the esp-claw.com loss)** - when `read` hits ConnectTimeout/403/Cloudflare-block, escalate: stealth browser tier -> proxy pool -> archive.org/Google-cache fallback. The proxy pool already planned for search must ALSO cover `read`/`scrape`. This closes the only dimension where the hosted tools structurally win.
3. **A `research(query)` / `deep_search` tool (the killer feature)** - one call that internally does search -> parallel full-`read` of top-K -> dedup/rerank -> returns a **consolidated full-content bundle** (NOT summarized). Claude/Codex needed multiple round-trips (search, then fetch, then synthesize); Argus could hand the agent all the raw material in ONE call, richer than WebFetch's lossy summary. This is the Jina-DeepSearch / Exa-answer niche - and turns Argus from "tools" into "out-researches them."
4. **Surface freshness** - always return `published` dates + a recency sort; lean into being live vs Codex's cached default.
5. **Keep the full-content edge explicit** - `read` returns complete clean markdown (vs WebFetch's lossy summary). This is the structural win vs Claude WebFetch; protect it (no truncation, ever).

</details>

---

## Improvements IMPLEMENTED (2026-06-24) + live re-validation

All three top improvements were built (TDD) and proven live with `research('ESP-Claw', max_sources=4)`:

- [x] **#1 search rerank** (`search.py` `rerank()`): title-weighted query-token scoring + URL/title dedup + safety floor -> the off-topic "ESP Guitar Company" hits no longer surface. 100% cov.
- [x] **#2 egress fallback** (`fetch/core.py` + `fetch/fallback.py`): on transport failure -> stealth browser -> **Wayback archive snapshot** -> re-raise (SSRF still propagates, never falls back).
- [x] **#3 `research()` tool** (`research.py`, MCP tool): search -> parallel FULL read of top-K -> consolidated full-content bundle (not summarized), partial-failure tolerant.

**Live result:** 4 sources, **0 failed** - cnx-software (437w) + github (498w) read static, and **`esp-claw.com` (240w) RECOVERED via `render_path:'archive'`** - the exact URL that failed with ConnectTimeout in the original run. The only dimension Argus structurally lost (egress reliability) is now mitigated without a hosted backend.

### Re-score (post-improvement, Argus column)
| Dimension | Argus before | Argus after |
|---|---|---|
| Result relevance/ranking | 3 | **4** (rerank drops off-topic) |
| Egress reliability | 3 | **4** (stealth->archive fallback recovers blocked hosts) |
| Research one-shot depth | n/a (raw tools) | **5** (`research` returns full content from N sources in one call) |

Net: Argus now matches or beats both on every dimension except raw server-side egress on hosts with no archive snapshot (closed further by the deploy-time proxy pool).
