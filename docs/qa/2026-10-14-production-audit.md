# Production audit 0.4.28 (2026-10-14)

> [!NOTE]
> Scope: end-to-end audit of Argus 0.4.27 for production readiness, done as the
> operator, a client agent, and an attacker. Three independent reviewers (security,
> code quality, performance) read the code; every finding below was verified by reading
> the real code path, and the headline ones were measured on the workstation before a
> line was changed. The 2026-10-04 reference study's open items (S1-S3, E1-E6) are
> folded in.

> [!NOTE]
> **Verification status: GREEN (2026-10-15).** The authoring session lost its command
> tools midway (a safety filter refused Bash, PowerShell, the sandbox and subagents), so
> the code and tests below were written without being run. The next session ran the
> [runbook](#verification-runbook): ruff clean, 1070 offline tests passed (2 skipped),
> SSRF gate 100% line + branch. Two trips, both fixed: `_looks_blocked` still gated its
> interstitial markers on raw page length and a 4 KB prefix, so 8 KB of inline JS hid
> "Just a moment..." (now gated on visible text, markers scanned on the bounded page);
> and three `test_cache` asserts compared the whole payload, which `get_stale` now
> extends with `cached_at`. The three `browser` tests could not run on that
> workstation (the Playwright CDN timed out); CI's browser job covers them.

## Daftar Isi

- [Baseline yang terukur](#baseline-yang-terukur)
- [Temuan live sebagai client](#temuan-live-sebagai-client)
- [Changed (0.4.28)](#changed-0428)
- [Deferred (with reason)](#deferred-with-reason)
- [Rejected (with reason)](#rejected-with-reason)
- [Verification runbook](#verification-runbook)

## Baseline yang terukur

| Check | Result (0.4.27, before changes) |
|---|---|
| `ruff check src tests benchmark` | clean |
| offline suite (`not browser and not slow and not network`) | 1026 passed |
| SSRF gate (`--cov=argus.security.ssrf --cov-branch --cov-fail-under=100`) | 100% (123 stmts, 34 branches) |
| `_visible_text_len` on 100 KB of `<` | 10.95 s (event loop stalled) |
| render `_visible_chars` on 20k unclosed `<script>` | 17.26 s |
| render `_visible_chars` on a page with a 4.8 KB inline script and "Just a moment..." | 4817 visible chars (script body counted; wall passed as content) |
| brotli, 200 MB of zeros | 329 bytes (httpx advertised `br` by default) |
| `urlsplit("http://example.com:99999/").port` | `ValueError` (reached the client unhandled) |

Workstation limits: no Docker, no VPS SSH key and no credentials file on this machine
(it is not the usual workstation), so nothing was run on the VPS or in the image. The
live service was exercised through the `argus` MCP client instead.

## Temuan live sebagai client

Calls against `https://argus.gifariksuryo.xyz/mcp` (0.4.27):

- `read https://example.com/` answered through `render_path: browser`. Not a bug:
  example.com now ships 171 visible chars plus a script that injects the multilingual
  body, so the thin-page escalation (< 200 chars) is correct.
- `batch_read` of an HTTP 500 came back as `{"code": "fetch_failed", "detail": "FetchError"}`:
  no status, no reason, no retry hint (reference study E1, now closed).
- `search(count=100000)` ran all five SearXNG pages (39 results) and cached under that
  key (E4, now clamped to 100).
- `smart_search("fastmcp stateless_http streamable http github repository")` routed to
  GitHub, sent the routing words along, got `no_results` and failed over to general with
  `degraded: true`. Closed: the signal words are stripped from the GitHub query.
- `extract_structured` with a non-matching selector answers `valid: false` (honest).
- `read ftp://...` -> `ssrf_blocked` (correct). The metadata-IP and own-VPS-IP probes were
  refused by the session's classifier, so S1 is verified by unit test, not live.
- `read_pdf`, `scholar_search`, `github_search`, `map_urls`, `find_similar`,
  `list_watches`, `research(quick)`: all answered correctly.

## Changed (0.4.28)

Each row names the reviewer finding, the fix and its test.

| # | Finding (severity) | Fix | Test |
|---|---|---|---|
| 1 | Quadratic regexes on the loop: `_visible_text_len`, render `_TAG_OR_SCRIPT` (Critical, measured 11 s / 17 s) | one linear lxml pass `static.visible_text_len`, 200k-char window, shared by both tiers; `_looks_blocked` moved to `static.py` | `test_visible_text_len_is_linear_on_hostile_markup`, `test_looks_blocked_does_not_count_script_bodies_as_visible_text` |
| 2 | `_TAG_OR_SCRIPT` closed on `</>`: script bodies counted as visible text, 200 interstitials passed (High) | same | same |
| 3 | Brotli bomb past the streaming cap (High, measured 200 MB -> 329 B) | `accept-encoding: gzip, deflate`; any other `content-encoding` -> `fetch_failed` | `test_brotli_body_is_refused_and_never_requested` |
| 4 | Own public IP passes the gate (S1, High) | `ARGUS_DENY_IPS` -> `config.DENY_IPS`, canonicalised; set via panel env (no compose default) | `test_is_blocked_ip_operator_deny_list`, `test_deny_ips_env_parsing` |
| 5 | NAT64 / IPv4-in-IPv6 not unwrapped (Low) | `_embedded_v4` (mapped, 6to4, Teredo, NAT64) re-checked | `BLOCKED_IPS` additions, `test_is_blocked_ip_allows_mapped_form_of_a_public_host` |
| 6 | WebRTC bypasses the egress proxy (S2) | `--webrtc-ip-handling-policy=disable_non_proxied_udp`, `--force-webrtc-ip-handling-policy` | `test_browser_config_routes_everything_through_the_proxy` |
| 7 | Bad port raises to the client; hostile `Location` too (High) | `validate_url` refuses (`ssrf_blocked`); redirect join -> `fetch_failed` | `test_validate_url_rejects[...99999]`, `test_unparseable_redirect_location_is_a_fetch_error` |
| 8 | Slow-drip body passes every per-read timeout (Medium) | `asyncio.wait_for(_stream_capped, timeout)` per hop | `test_slow_drip_body_hits_the_hop_deadline` |
| 9 | 2xx anti-bot walls served as content on the static path (E3) | `cf-mitigated: challenge`, `x-amzn-waf-action`, body heuristic -> `blocked_by_antibot` | `test_2xx_challenge_headers_are_an_antibot_block`, `test_200_interstitial_body_is_an_antibot_block` |
| 10 | Unmapped exceptions reach the client as raw `str(exc)` (Med-High) | middleware catch-all -> `internal_error` (sanitized); stealth launch wrapped as `render_failed`, launched inside the permit | `test_middleware_turns_an_unhandled_exception_into_internal_error` |
| 11 | No ceiling on `read`/`screenshot`; `batch_read` no batch deadline; PDF extraction uncancellable (Critical/High perf) | `read` <= `timeout + 45`; `screenshot` <= `timeout`; `batch_read(timeout)` returns finished + flags pending (`budget_exhausted`); `read_pdf` abandons past `timeout` | `test_read_has_a_hard_ceiling`, `test_screenshot_has_a_deadline`, `test_batch_read_deadline_returns_finished_and_flags_the_rest`, `test_read_pdf_extraction_has_a_deadline` |
| 12 | `extract_structured`: unbounded list, client XPath on the loop (F3/F7) | cap 200 + per-call budget; selectors in a thread with 15 s `wait_for` | `test_extract_structured_caps_the_url_list` |
| 13 | `watch`: selector miss hashes the whole page (S3); XPath on the loop; broken webhook stalls the poller; damaged store row crashes startup; webhook logged in full (F8) | `selector_not_found` keeps the hash; selector off-loop + validated at registration; `poll_due` guards delivery; `_load` tolerant; host-only logging | `test_check_selector_no_match_is_an_error_not_a_whole_page_hash`, `test_poll_due_selector_miss_keeps_the_baseline`, `test_poll_due_survives_a_delivery_crash`, `test_store_tolerates_a_damaged_row`, `test_watch_rejects_a_broken_selector` |
| 14 | Wayback copy cached as live and watched (Medium) | `read` skips cache for `render_path: archive`; `check_watch` -> `archive_only` | `test_read_does_not_cache_a_wayback_copy`, `test_check_wayback_copy_is_not_the_live_page` |
| 15 | Auth fails open; JWT without issuer/audience (F4, F12) | lazy `app` refuses without auth unless `ARGUS_ALLOW_NOAUTH=1`; JWKS requires both | `test_http_app_fails_closed_without_auth`, `test_auth_jwt_requires_issuer_and_audience` |
| 16 | Actionable errors + stale flag (E1, E2) | `_fetch_err`: `http_status`, `reason`, `retryable`; `stale: true` + `cached_at` | `test_read_fetch_errors_are_actionable`, `test_read_serves_stale_copy_flagged`, `test_get_stale_returns_expired_payload` |
| 17 | Search: `count` unclamped, list-vs-string key collision (E4, #9) | clamp 1-100; `multi` in the key | `test_search_count_is_clamped_and_list_queries_cache_apart` |
| 18 | `smart_search` sends routing words to GitHub (live) | `_GITHUB_SIGNALS` stripped | `test_smart_search_routes` |
| 19 | Half-open breaker admits a burst (#10) | re-arm `opened_at` at half-open | `test_half_open_admits_exactly_one_trial` |
| 20 | Corrupt gzip blob raises every read for 7 days (#8) | `zlib.error` in the self-heal path | `test_corrupt_gzip_blob_self_heals` |
| 21 | Metrics label cardinality from client names (F10) | only real tool names, else `unknown` | `test_metrics_middleware_bounds_label_cardinality` |
| 22 | Sitemap XML parsed on the loop, no size cap (F6) | `to_thread` + 8 MiB cap | `test_oversized_plain_sitemap_skipped`, `test_gzip_bomb_sitemap_skipped` |
| 23 | Shared thread pool: DNS starves behind extraction/ONNX; concurrent ONNX runs multiply the arena (F5) | dedicated `_DNS_POOL`; `_EMBED_LOCK` | covered by the existing SSRF/semantic suites |
| 24 | Cache fsync per commit; hourly purge on the loop (F6 perf) | `synchronous=NORMAL`; `to_thread(purge)` | existing cache tests |
| 25 | Research highlights run after the deadline (F9) | 20 s budget | existing `test_research_highlights` |
| 26 | Tool ergonomics (E5, E6) | `anthropic/alwaysLoad` on read/search/research; parameter descriptions | `test_entry_point_tools_are_always_loaded`, `test_parameter_descriptions_reach_the_schema` |

## Deferred (with reason)

| Item | Reason |
|---|---|
| Container hardening: `cap_drop: [ALL]`, `pids_limit`, `read_only` + tmpfs (F11) | No Docker host in this session to prove Chromium still launches; a wrong guess breaks the auto-deploy. Do it with the image at hand: add `cap_drop: [ALL]` and `pids_limit: 2048`, `docker compose up`, run `scrape` once. |
| HTML body cap below 32 MiB, global in-flight fetch semaphore (F4 perf) | 16 parallel 32 MiB pages can OOM the 3 GB container; the batch/research fan-outs are already bounded (16 / 10). Measure peak RSS under `batch_read` of large pages on the VPS before choosing a cap. |
| Raw-HTML page cache, single-flight, research sharing the read cache (F7 perf) | Latency win, not a stability issue; needs the B1 benchmark numbers first. |
| Crawl: `max_session_permit`, normal-tier recycle on wedge, page count in `/metrics` (F8 perf) | crawl4ai internals unverified here; needs the real browser (CI browser job). |
| Throttle: prune `_hosts`, slot leak on cancellation (F10 perf) | Slow growth, small objects; revisit with live memory numbers. |
| `backend` field exposes the SearXNG hostname (F9 sec, Low) | The gate blocks that host anyway; changing the field breaks benchmark readers. |
| Wayback lookups disclose the full URL (F14, Low) | The URL is what the agent asked to read; stripping the query would miss most snapshots. |
| Origin/Host validation for the loopback no-auth mode (F15, Low) | Loopback only; `ARGUS_ALLOW_NOAUTH` is now explicit. |
| Event-loop lag gauge in `/metrics` (perf suggestion) | Would have caught #1; cheap, but adds a task; do after the suite is green. |
| Reference study P3 (B1 fetch benchmark, A2 smarter escalation, T5/T6/B4 extraction and rerank) | Measure-first items that need the VPS. |

## Rejected (with reason)

- Rejecting `429`/`503` from the negative cache (reviewer note): a 429 from a wall and a
  429 rate limit look the same from here; 10 minutes of fail-fast is the cheaper error.
- Stripping `script` text via a tempered regex instead of lxml: still quadratic on k
  unclosed openers; lxml is linear and already a dependency.
- Making `read`'s ceiling exactly `timeout`: the ladder is the product (static 5 s, then
  one 45 s browser rung, then Wayback); `timeout + 45` keeps the ladder and ends the hang.

## Verification runbook

Run from the repo root (venv rebuilt this session from `requirements.lock`, fastmcp
4.0.10, so it matches CI and the image):

```powershell
.\.venv\Scripts\ruff.exe check src tests benchmark
.\.venv\Scripts\python.exe -m pytest -q -m "not browser and not slow and not network"
.\.venv\Scripts\python.exe -m pytest tests/test_ssrf.py --cov=argus.security.ssrf --cov-branch --cov-fail-under=100 -q
.\.venv\Scripts\python.exe -m pytest -q -m "browser or slow" -rs   # needs Chromium + the model
```

Known places a first run may trip, in order of likelihood:

1. `from fastmcp.tools import ToolResult` / `mcp.auth` attribute: both exist in 4.1.0;
   confirm in 4.0.10 (else `fastmcp.tools.tool.ToolResult`, `mcp._auth`).
2. `test_looks_blocked_does_not_count_script_bodies_as_visible_text`: depends on
   crawl4ai's detector or the short-page markers flagging "Just a moment..."; the
   `visible_text_len` assertions are the mechanism and must hold regardless.
3. `test_slow_drip_body_hits_the_hop_deadline`: relies on httpx MockTransport honouring
   an async generator body with `asyncio.sleep` inside (an existing test already streams
   an async generator).
4. Ruff import ordering in `server.py` (`from .watch import WatchStore, _select_value,
   poll_due`).

After green: commit (`feat: production audit 0.4.28 ...`), push (the Easypanel webhook
deploys), then check `https://argus.gifariksuryo.xyz/health` and that the Easypanel env
carries no `ARGUS_ALLOW_NOAUTH` and does carry `ARGUS_TOKEN` (the app now refuses to
start otherwise, which is the point).

---

Copyright 2026 PT Surya Inovasi Prioritas (SURIOTA).
