# Agent-Reach vs Argus - gap audit

Date: 2026-11-23. Agent-Reach clone: `Panniantong/Agent-Reach` @ `a8ab1e9` (2026-11-22), version 1.6.1 (`pyproject.toml`).
Argus: working tree at `main` (0.4.48 per `docs/STATUS.md`). Analysis only, nothing in the Argus repo was changed.
Agent-Reach code was read, never executed. All "live" checks below were made through the production Argus MCP
(`read` / `batch_read` / `search`), so they reflect the primary server's datacenter IP.

Path prefixes: `AR:` = Agent-Reach repo root, `ARGUS:` = `C:\Users\Administrator\Pictures\Argus Web MCP`.

---

## (a) What Agent-Reach is

- A **local Python CLI** (`agent-reach <channel> <action>`, one JSON envelope per call) plus an agent Skill file,
  meant to run on the user's own desktop next to Claude Code / Codex / Cursor. Not an MCP server: the MCP entry was
  removed in 1.6 (`AR:CHANGELOG.md` 1.6.0 "removed ... MCP server entry", `AR:docs/plans/2026-10-20-shared-cloak-browser.md` step 1).
- Architecture: channel registry (`AR:agent_reach/channels/__init__.py`), each channel declares actions with an
  **ordered backend list**; the dispatcher (`AR:agent_reach/runtime/dispatch.py`) tries backends in order, maps every
  failure to a unified code (`AR:agent_reach/runtime/result.py`: NEED_SETUP, NEED_LOGIN, UPSTREAM_BROKEN, TIMEOUT,
  RATE_LIMITED, PLATFORM_BLOCKED, NOT_FOUND, BAD_INPUT, BUSY) with an agent-facing hint, pauses failing backends and
  rate-limited channels in a JSON state ledger (`AR:agent_reach/runtime/state.py`).
- It is mostly a **wrapper/installer around third-party tools**: OpenCLI (Node CLI + Chrome extension + daemon) for
  Reddit/X/XHS/Facebook/Instagram/LinkedIn/Boss, bili-cli, boss-agent-cli, yt-dlp, gh CLI, Exa's hosted MCP, Jina
  Reader, Groq/OpenAI Whisper (`AR:agent_reach/runtime/tools.py` `TOOLS`, `AR:pyproject.toml`).
- Login channels run in one **dedicated headless browser** (CloakBrowser preferred, Chrome fallback) and copy the
  site's cookies **from the user's own Chrome** through the OpenCLI Browser Bridge (`AR:agent_reach/runtime/session_reuse.py`,
  `AR:agent_reach/backends/opencli.py::export_site_cookies`, `AR:docs/cloakbrowser.md`).
- "Public-first": for channels not yet set up, search/read first go through the web channel scoped with `site:` and
  results are labelled `content_scope`, `completeness_verified: false` (`AR:agent_reach/runtime/public_web.py`).
- Release gate: every channel action has a `live_test`; a tag cannot ship unless a live-acceptance manifest bound to
  the source SHA-256 passed or waived each action (`AR:agent_reach/channels/base.py::Action.live_test`,
  `AR:scripts/check_release_acceptance.py`, `AR:.github/release-acceptance.json`).
- Dependencies (core): requests, loguru, pyyaml, `yt-dlp[default]==2026.8.19` (`AR:pyproject.toml`).
- **License: MIT** (`AR:LICENSE`, "Copyright (c) 2025 Agent Eyes"). Caveat: CloakBrowser's browser binary has its own
  non-MIT binary license (`AR:docs/cloakbrowser.md` last paragraph). OpenCLI, bili-cli, boss-agent-cli licenses:
  unverified (not in this repo).

---

## (b) Platform / capability comparison

"Live" = checked today through production Argus from the primary server IP.

| Platform / capability | Agent-Reach method (file) | Argus today (file) | Gap? |
|---|---|---|---|
| Web search | Exa hosted MCP `mcp.exa.ai`, keyless (`AR:agent_reach/channels/web.py::_exa_search`) | SearXNG, 6 engines, rerank, categories general/news/science/it (`ARGUS:src/argus/search.py`) | No. Argus stronger (self-hosted, unlimited). |
| Web read | Jina Reader `r.jina.ai` -> Exa `web_fetch_exa` fallback (`AR:agent_reach/channels/web.py::_jina_read/_exa_read`) | static -> trafilatura/rs-trafilatura -> browser -> stealth -> Wayback, docs/OCR (`ARGUS:src/argus/fetch/core.py`) | No. Argus stronger. |
| Anti-bot page detection | `_is_antibot_page` (Jina captcha + Cloudflare markers) and `_readable` (login/challenge title and login-only body rejected) (`AR:agent_reach/channels/web.py`, `AR:agent_reach/runtime/public_web.py`) | `_looks_blocked` markers (`ARGUS:src/argus/fetch/render.py`) | **Yes (partial)**. Live: `read("https://xueqiu.com/S/SH600519")` returned an Aliyun WAF challenge (`{"_waf_bd8ce2ce37": ...}` + obfuscated blob) as status 200 content, `word_count 1`, `extraction_quality 0.9`, and cached it. Soft-404s also pass as content: `reddit.com/r/esp32/.rss` -> "# Not Found", `linkedin.com/jobs/view/4000000000` -> "Page not found" (status 200, browser path). |
| GitHub search | gh CLI -> REST `/search/repositories` (`AR:agent_reach/channels/github.py`) | `github_search` repos/code/issues, token (`ARGUS:src/argus/gh_search.py`) | No. Argus wider. |
| GitHub read (repo, issue, PR + comments) | REST `/repos/{o}/{r}` + raw README; `/issues/{n}` + `/issues/{n}/comments` (`AR:agent_reach/channels/github.py::_read`) | Generic HTML read only, no adapter (`ARGUS:src/argus/fetch/adapters.py` has none) | **Yes**. Live: `read("https://github.com/astral-sh/uv/issues/1")` returned mostly whitespace plus sidebar ("Assignees No one assigned ..."); issue body/comments not reliably extracted. |
| YouTube video info + transcript | yt-dlp `--dump-json`, `--write-subs/--write-auto-subs` VTT (`AR:agent_reach/channels/youtube.py`) | Android `youtubei/v1/player` + timedtext in-process, SSRF-guarded (`ARGUS:src/argus/fetch/adapters.py::fetch_youtube`) | No. Equivalent; Argus has no subprocess and no new dep. |
| YouTube search | yt-dlp `ytsearchN:` (`AR:agent_reach/channels/youtube.py::_ytdlp_search`) | None. `search` categories exclude videos (`ARGUS:src/argus/search.py` `_VALID_CATEGORIES`) | **Yes**. Live: `read("https://www.youtube.com/results?search_query=esp32+modbus")` works through the browser but returns one noisy blob, not a result list. |
| Speech-to-text (YouTube, Xiaoyuzhou podcasts) | yt-dlp audio + ffmpeg chunks -> Groq/OpenAI Whisper API with user key (`AR:agent_reach/transcribe.py`, `AR:agent_reach/scripts/transcribe_xiaoyuzhou.sh`) | None (local audio rejected on CPU cost, `ARGUS:docs/qa/2026-10-31-reference-study-2.md` "Ditolak") | Yes, but **do not adopt** (external key API, see d). |
| Reddit thread read | OpenCLI in logged-in browser; public-web fallback via Jina (`AR:agent_reach/channels/reddit.py`, `AR:agent_reach/runtime/dispatch.py` last-resort block) | Thread Atom feed adapter with 60 s cool-down (`ARGUS:src/argus/fetch/adapters.py::fetch_reddit`) | No for threads. |
| Reddit search / subreddit listing | OpenCLI `search`, `subreddit` (login) (`AR:agent_reach/channels/reddit.py`) | None. Live: `reddit.com/search.rss` -> 429 `blocked_by_antibot`; `reddit.com/r/esp32/.rss` -> "Not Found" page | Yes, but only closable with a logged-in account or a residential IP (see d). |
| X / Twitter | OpenCLI in logged-in browser, "experimental" (`AR:agent_reach/channels/twitter.py`) | `x_search` GraphQL with one cookie account + FxTwitter + index; `read` thread/profile (`ARGUS:src/argus/x/`) | No. Argus stronger and server-native. |
| Bilibili | bili-cli -> public API `api.bilibili.com/x/web-interface/view`, `/popular`; subtitles need SESSDATA login (`AR:agent_reach/channels/bilibili.py`) | Generic read only. Live: `read("https://www.bilibili.com/video/BV1GJ411x7h7")` -> "Huh? Video missing?" redirect shell. Live: `api.bilibili.com/x/web-interface/view?bvid=BV1xx411c7mD` and `/popular` answer 200 JSON from the primary server | **Yes** (video page); API route verified reachable. Whether BV1GJ411x7h7 itself still exists is unverified (it is Agent-Reach's own `live_test`). |
| V2EX | Public JSON API `/api/topics/hot.json`, `/topics/show.json`, `/replies/show.json`, `/members/show.json`, curl TLS fallback (`AR:agent_reach/channels/v2ex.py`) | Generic HTML read. Live: `v2ex.com/t/1000` readable with replies but as table noise; `api/topics/hot.json` answers 200 | Minor (cleanliness + hot/node listing). |
| Xueqiu quotes / stock search | Tencent `qt.gtimg.cn/q=` (GBK, `~`-split) and `smartbox.gtimg.cn` (`AR:agent_reach/channels/xueqiu.py::_tencent_quote/_tencent_search`) | None. Live: `qt.gtimg.cn/q=sh600519` answers 200 from the primary server | Yes (niche: CN/HK/US equities). |
| Xueqiu community (hot posts/stocks) | Cookie jar from dedicated browser `xq_a_token` (`AR:agent_reach/channels/xueqiu.py::_import_from_browser`) | None; page is behind Aliyun WAF (live, above) | Yes, login-only, do not adopt. |
| LinkedIn jobs | OpenCLI `search`/`job-detail`; public-web first; URL normalised to `/jobs/view/<id>` (`AR:agent_reach/channels/linkedin.py::_job_url`) | Generic read. Live: `au.linkedin.com/jobs/python-developer-jobs` read statically (958 words, rs-trafilatura listing) | No for public pages. Logged-in search: not adopted. |
| Xiaohongshu, Instagram, Facebook, Boss Zhipin | OpenCLI / boss-agent-cli in logged-in dedicated browser (`AR:agent_reach/channels/xiaohongshu.py`, `instagram.py`, `facebook.py`, `boss.py`, `AR:agent_reach/backends/boss_native.py`) | None (Instagram/LinkedIn/TikTok rejected earlier: `ARGUS:docs/qa/2026-10-31-reference-study-2.md`) | Yes, login-only, do not adopt. |
| Hacker News, Telegram, Polymarket, StackExchange, npm, PDFs/documents, OCR, feeds, scholar, trading | none | adapters and tools (`ARGUS:src/argus/fetch/adapters.py`, `scholar.py`, `trading/`, `extract/`) | Argus-only. |
| Caching | none for content (only `lru_cache` on a code signature, `AR:agent_reach/runtime/manage.py:61`) | content-addressed SQLite+disk cache (`ARGUS:src/argus/cache.py`) | Argus-only. |
| Backend fallback + pause | ordered backends; pause TIMEOUT 60 s, UPSTREAM_BROKEN 120 s, NEED_SETUP 60 s keyed on a code fingerprint; channel cool-down 3600 s on RATE_LIMITED (`AR:agent_reach/runtime/dispatch.py` `BACKEND_PAUSE_SECONDS`, `cooldown`, `_remember`) | per-host throttle + circuit breaker, per-engine cool-down, Reddit adapter cool-down, FF `Retry-After` (`ARGUS:src/argus/fetch/throttle.py`, `search.py`, `adapters.py`, `trading/forexfactory.py`) | Minor: Argus honours `Retry-After` only for FairEconomy. |
| Error contract | unified code + `hint` + `retry_after_seconds` (`AR:agent_reach/runtime/result.py::DEFAULT_HINTS`, `envelope`) | `{error, code, detail, http_status, reason, retryable}` (`ARGUS:src/argus/models.py`) | Minor: no `retry_after_seconds`, no `hint`. |
| Health / doctor / regression | per-action `live_test`, `agent-reach check`, `doctor` states incl. `regressed` (passed last time, fails now) (`AR:agent_reach/runtime/manage.py::run_checks`, `regressed`) + release gate (`AR:scripts/check_release_acceptance.py`) | `/health` engines_dead, x_account, egress; daily `reachability.py` site map (13 sites, root/page level only); `mcp_smoke.py` by hand | **Yes**: no per-adapter canary with a "regressed" signal (YouTube `_YT_CLIENT_VERSION` and X queryId drift are documented as manual bumps in `ARGUS:src/argus/fetch/adapters.py`). |
| SSRF | lexical check only, no DNS resolve, no IP pin (`AR:agent_reach/utils/url.py::normalize_public_http_url`); fetches mostly done by Jina/Exa anyway | resolve-then-validate, IP pin, per-hop, browser egress proxy, 100% coverage (`ARGUS:src/argus/security/ssrf.py`, `egress.py`) | Argus stronger. |
| Install / ops | `uv tool install`, private tool venvs, `add/remove/update/uninstall`, skill linking (`AR:agent_reach/runtime/tools.py`, `manage.py`) | Docker/Coolify server, remote HTTP MCP | Not applicable (different deployment model). |

---

## (c) Ranked adoption list

Constraints applied: no paid web API, no LLM dependency, SSRF resolve-then-validate untouched (every new endpoint goes
through the existing guarded client / `fetch_static(..., api=True)`), MIT-compatible ideas only (no code copy needed;
all items below are small enough to write fresh), stdlib / existing deps first.

### P1

**P1-1. Close the "wall served as content" hole (Aliyun WAF + soft-404).**
- Evidence: live xueqiu Aliyun WAF challenge returned 200, cached, `extraction_quality 0.9`; reddit `.rss` "Not Found" and LinkedIn "Page not found" returned `ok`.
- Technique from Agent-Reach: explicit challenge markers checked on the first bytes (`AR:agent_reach/channels/web.py::_is_antibot_page`) and a content gate that rejects pages whose title is a wall/login/"not found" and whose body is short (`AR:agent_reach/runtime/public_web.py::_readable`).
- Approach: add Aliyun WAF signatures (`"_waf_` JSON key, `acw_sc__v2` / `acw_tc` challenge script, unverified exact markers: capture the real body from the server first) to `_looks_blocked` in `ARGUS:src/argus/fetch/render.py` so it raises `blocked_by_antibot` (not cached, negative cache, Wayback fallback). For soft-404: when title or first heading matches `not found|page not found|404` and visible text is short, return `not_found` (or at least `metadata.soft_404: true`) and do not cache. Leave one golden-file test per marker.
- Effort S. No new dep. Risk none. Datacenter: n/a (detection only).

**P1-2. GitHub read adapter (repo, issue, PR with comments).**
- Evidence: live uv#1 issue read lost body and comments (GitHub's React issue view).
- Agent-Reach: `AR:agent_reach/channels/github.py::_read` (`/repos/{o}/{r}`, raw README via `Accept: application/vnd.github.raw`, `/issues/{n}` + `/issues/{n}/comments`).
- Approach: `github_target(url)` + `fetch_github(url, client, timeout)` in `ARGUS:src/argus/fetch/adapters.py`, wired in the first adapter loop of `ARGUS:src/argus/fetch/core.py`; reuse `_token()`/`_headers()` from `ARGUS:src/argus/gh_search.py` (existing `ARGUS_GITHUB_TOKEN`). Paginate comments (`per_page=100`, follow `Link`), PR review comments optional. Do not copy Agent-Reach's silent `_README_MAX_CHARS = 50_000` cut: Argus must not truncate silently. Also handle `blob/` URLs via `raw.githubusercontent.com` (already readable statically; check).
- Effort S (half a day). No new dep. Risk: 5000 req/h with token, none ToS. Datacenter: yes (api.github.com is IP-agnostic with a token).

**P1-3. Per-adapter canaries with a `regressed` signal.**
- Agent-Reach: every action carries a `live_test` input (`AR:agent_reach/channels/base.py::Action.live_test`), `check` runs them, `doctor` flags `regressed` = passed in previous check, fails now (`AR:agent_reach/runtime/manage.py::run_checks/save_checks/regressed`); release blocked without a passing manifest bound to the source hash (`AR:scripts/check_release_acceptance.py`).
- Approach: extend the existing daily job in `ARGUS:src/argus/reachability.py` with one known-good input per adapter (YouTube player + caption track, X GraphQL search, Telegram channel, HN item, Polymarket event, Reddit thread feed, StackExchange, npm, FairEconomy, CFTC, scholar S2/OpenAlex/CrossRef, GitHub adapter from P1-2); store `{at, ok, previous_ok}`; expose `adapters_regressed` in loopback `/health` and alert from `uptime.yml` like `engines_dead`. This catches YouTube `_YT_CLIENT_VERSION` and X queryId drift before users do. Optional: make `mcp_smoke.py all` output a manifest tied to `git rev-parse HEAD` and refuse `coolify-set-images.php` bump without it.
- Effort S-M. No new dep. Risk: a few extra requests per day (keep the X canary to 1 call/day to protect the cookie account). Datacenter: runs from the server by design.

**P1-4. YouTube search.**
- Agent-Reach: `yt-dlp ytsearchN:` (`AR:agent_reach/channels/youtube.py::_ytdlp_search`).
- Approach (cheapest first): (1) SearXNG ships a keyless `youtube_noapi` engine: enable it in `ARGUS:deploy/searxng/settings.yml`, add `"videos"` to `_VALID_CATEGORIES` and the `category` enum, measure with `scripts/check_engines.py` from the primary. (2) If it does not answer from the server IP, POST `youtubei/v1/search` with the same Android client context already used by `_yt_player` (`ARGUS:src/argus/fetch/adapters.py`), parse `videoRenderer` items. Do not add yt-dlp.
- Effort S. No new dep. Risk: YouTube 429 on the shared IP (memory: direct works, WARP 0/5, keep it off WARP). Datacenter: **unverified** for search (player/transcript verified 5/5 in `ARGUS:docs/qa/2026-10-31-reference-study-2.md` M8).

### P2

**P2-1. Bilibili adapter via public API.**
- Agent-Reach: `AR:agent_reach/channels/bilibili.py::_api_read/_api_hot`, `_bvid` (BV regex, `b23.tv` HEAD redirect), headers UA + `Referer: https://www.bilibili.com/`.
- Approach: `bilibili_target` -> `api.bilibili.com/x/web-interface/view?bvid=` -> record (title, owner, pubdate, desc, stat, duration) as an `api` render path, `metadata.structured`. Resolve `b23.tv` through the guarded redirect path, not a raw HEAD. Skip subtitles (need SESSDATA login).
- Effort S. No new dep. Risk: Bilibili risk control (Agent-Reach notes 412 for yt-dlp, `bilibili.py` module docstring). Datacenter: **yes**, verified today (`view` and `popular` 200 JSON).

**P2-2. `Retry-After` and hints in every error.**
- Agent-Reach: `retry_after_seconds` + per-code `hint` (`AR:agent_reach/runtime/result.py::DEFAULT_HINTS`, `AR:agent_reach/runtime/dispatch.py::cooldown`).
- Approach: when an upstream answers 429/503 with `Retry-After`, put `retry_after_seconds` in `err(...)` detail and let the existing per-host breaker honour it (generalise the FairEconomy logic in `ARGUS:src/argus/trading/forexfactory.py`). A one-line `hint` per error code in `ARGUS:src/argus/models.py` helps the consuming agent stop retry loops.
- Effort S. No dep. Risk none.

**P2-3. Scope labels on partial adapter output.**
- Agent-Reach: `content_scope`, `access`, `completeness_verified: false` on public-route items (`AR:agent_reach/runtime/public_web.py::attempt`), `raw.exhaustive=False` on comments (`AR:agent_reach/channels/xiaohongshu.py::_opencli_comments`).
- Approach: set `metadata.content_scope` on adapters that by design return a slice (Reddit feed = top comments only, Telegram = latest ~20 posts, X replies page 1) so agents do not claim "all comments".
- Effort S. No dep.

**P2-4. V2EX adapter.**
- Agent-Reach: `AR:agent_reach/channels/v2ex.py` (`/api/topics/show.json?id=`, `/api/replies/show.json?topic_id=`).
- Approach: same adapter pattern; output post + replies cleanly instead of table markup. Low audience value for SURIOTA; do only if Chinese dev forums matter.
- Effort S. No dep. Datacenter: **yes**, `hot.json` verified today.

### P3

**P3-1. Tencent quote endpoint for CN/HK/US equities** (`AR:agent_reach/channels/xueqiu.py::_tencent_quote/_tencent_search`, GBK decode, `~`-split fields 1..34). Only if Artha/Aurix needs equity quotes; no forex/gold. Effort S. Datacenter: **yes**, `qt.gtimg.cn/q=sh600519` verified today. Field map needs the >=99% golden-file gate before any trading use.

**P3-2. LinkedIn job URL normalisation** (`AR:agent_reach/channels/linkedin.py::_job_url`: regional hosts + slug -> `www.linkedin.com/jobs/view/<id>`) as a cache-key normaliser, same idea as the X URL-spelling dedup in 0.4.39. Effort XS. Public pages already read statically (verified).

**P3-3. Hosted reader (Jina `r.jina.ai`) as a last-resort egress for IP-blocked hosts** (reddit listings, medium, quora, investing) (`AR:agent_reach/channels/web.py::_jina_read`). Owner decision only: it is a third-party commercial service on a free keyless tier (limits unverified), leaks every URL to Jina, and contradicts the self-hosted principle in `ARGUS:CLAUDE.md`. Argus would still validate the URL first; SSRF on Argus infra is unaffected. Not recommended by default.

---

## (d) What Agent-Reach does worse, or what Argus should NOT adopt

- **OpenCLI + dedicated browser + cookie copy from the user's Chrome** (`AR:agent_reach/backends/opencli.py`, `AR:agent_reach/runtime/session_reuse.py`, `AR:agent_reach/runtime/browser.py`). Built for a desktop with a human and a personal Chrome profile. On a datacenter server it means holding real account cookies for Reddit, XHS, Instagram, Facebook, LinkedIn: account lock and ToS risk per platform (their own README tells users to use a secondary account), plus Node + extension + daemon on the box. Argus already runs one cookie account for X under an owner-accepted risk; extending that pattern to more platforms needs a per-platform owner decision, not a framework.
- **CloakBrowser** (`AR:agent_reach/backends/cloak.py`): binary under a separate non-OSS license (`AR:docs/cloakbrowser.md`), and Argus measured that blocks happen at the IP before JS (Patchright 0.4.21, Camofox and invisible_playwright rejected in `ARGUS:docs/qa/2026-10-31-reference-study-2.md`).
- **Login-only channels** (Xiaohongshu, Instagram, Facebook, Boss Zhipin, Xueqiu community, Reddit search/subreddit): need accounts; Agent-Reach itself ships several of them "experimental" or untested (`AR:.github/release-acceptance.json` waivers for Instagram, Reddit subreddit, Bilibili subtitles, Xiaoyuzhou).
- **Whisper transcription via Groq/OpenAI keys** (`AR:agent_reach/transcribe.py`, `AR:agent_reach/scripts/transcribe_xiaoyuzhou.sh`; the script's `--polish` even calls a Groq Llama model). External key APIs (OpenAI paid, Groq third-party) and an LLM in the path; Argus is tools-not-brain and rejected local audio on CPU cost.
- **yt-dlp as a dependency** (`AR:pyproject.toml`): subprocess outside Argus's SSRF guard and egress proxy, large and fast-moving (pinned `2026.8.19`), and Agent-Reach dropped it for Bilibili after 412 blocks (`AR:agent_reach/channels/bilibili.py` docstring). Argus's in-process youtubei adapter already covers metadata + transcripts.
- **Exa hosted MCP and Jina as primary search/read** (`AR:agent_reach/channels/web.py`): rate-limited third-party services (the 1.6.1 changelog fixes a Cloudflare block on Exa by changing the User-Agent), no full-content guarantee (`maxCharacters: 20000`). Argus's SearXNG + extraction ladder is stronger and owned.
- **Weaker SSRF**: `AR:agent_reach/utils/url.py::normalize_public_http_url` is lexical (literal IP and hostname deny list, no DNS resolution, no IP pinning, no per-redirect re-check). Do not port it; Argus's resolve-then-validate stays the only gate.
- **Silent truncation**: README cut at 50,000 chars (`AR:agent_reach/channels/github.py` `_README_MAX_CHARS`), Exa read capped at 20,000 chars, Xueqiu hot post text `[:200]` (`AR:agent_reach/channels/xueqiu.py::_parse_hot_posts`), YouTube subtitles keep one language with no flag. Conflicts with Argus's no-silent-truncation gate.
- **No content cache, CLI-process state in a JSON ledger with file locks** (`AR:agent_reach/runtime/state.py`): fine for a single-user CLI, wrong model for a server.
- **X/Twitter via OpenCLI**: experimental there; Argus's GraphQL + FxTwitter + index chain (`ARGUS:src/argus/x/`) is more capable.
- **Skill-file distribution / installer machinery** (`AR:agent_reach/runtime/manage.py`, `tools.py`, skill linking): solves local install for many agents; Argus is a remote MCP with zero client install, so nothing to take.

Unverified items (stated where they appear): exact Aliyun WAF marker set, `youtube_noapi` / youtubei search reachability from the primary IP, whether BV1GJ411x7h7 still exists, Jina free-tier limits, licenses of OpenCLI / bili-cli / boss-agent-cli, and the "Agent Reach MCP" tools named in `AR:agent_reach/skill/SKILL_en.md` (`creator_inspect`, `people_resolve`, `trend_scan`), which are not in this repo (possibly a separate hosted product).
