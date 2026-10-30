# Argus X (Twitter) module - design

Status: draft for owner review (2026-10-30). Next step after approval: implementation plan (writing-plans).

## Goal

Let Argus users find and read X posts by keyword, profile and thread, as one Argus-owned module
built from the best parts of twscrape, Scweet and Tweety (all MIT), without a paid API.

Success:

- `x_search("esp32 modbus")` returns relevant posts with full text, author, date and metrics.
- `read("https://x.com/<user>/status/<id>")` returns one clean post (plus thread when an account
  is configured), not the duplicated page text it returns today.
- With no X account configured everything above still works (index tier); with one account,
  `x_search(mode="latest")` is near real-time.
- An X-side change (queryId rotation) heals itself without a release; an expired or locked
  account shows up on `/health`, never as silently empty results.

## Non-goals

- No automated account creation, captcha solving, or account pools bought from third parties.
  The owner creates one account by hand.
- No writes (post, like, follow, DM), no followers/following graph, lists, communities, Spaces.
- No password login and no stored credentials: cookies only.
- No paid X API (Argus rule). No LLM.

## Evidence (research 2026-10-30)

| Finding | Source |
|---|---|
| Official X API is pay-per-use only: $0.005 per post read, no free tier, 3M reads/month cap | docs.x.com/x-api/getting-started/pricing |
| twscrape, Scweet, Tweety work; twikit, snscrape, Twint, ntscraper, Nitter are dead or archived | Repo state via GitHub API; Sorsa 2026 survey |
| Every library breaks every 2-4 weeks (GraphQL queryId rotation, transaction-id changes) | git logs of all three (local clones) |
| `site:x.com <keyword>` through Argus SearXNG returns relevant posts, some days old | Live `search` test |
| `api.fxtwitter.com` returns full post JSON (expanded URLs, media, metrics) and profiles, no login | Live `read` test |
| X ToS (2026-10-09) forbid scraping without consent; liquidated damages only above 1M posts per 24 h | X Terms, lilachbullock.com summary |

Library teardown (read-only, nothing executed):

| | twscrape | Scweet | Tweety |
|---|---|---|---|
| License | MIT | MIT | MIT (`src/tweety/LICENSE`) |
| queryIds | hardcoded, release-gated | hardcoded + opt-in live scrape | hardcoded |
| Pacing | none (locks only) | token bucket 50 req/900 s, 300 req/day | none |
| Transaction-id | own port (MIT, from iSarabjitDhiman/XClientTransaction) | third-party package | own port (credits TweeterPy) |
| Red flags | PostHog telemetry on by default | none found | `eval`, `shell=True`, global httpx monkeypatch |

## Architecture

Package `src/argus/x/`, one job per file:

| File | Job | Origin |
|---|---|---|
| `index.py` | Layer 0: SearXNG `site:x.com` search, status-URL filter, FxTwitter enrichment | new |
| `fxtwitter.py` | FxTwitter client: post and profile JSON -> Argus post model | new |
| `xclid.py` | `x-client-transaction-id` generator | vendored from twscrape (MIT, both notices kept) |
| `manifest.py` | Live queryId + `features` discovery from x.com JS bundles, cached, refreshed on 404 / error 336 | idea from Scweet, rewritten |
| `session.py` | Cookie account (`auth_token`, `ct0`), persists rotated `ct0`, bootstrap from `auth_token` only | idea from Tweety, rewritten |
| `limiter.py` | Token bucket, min gap + jitter, daily cap, cooldown table per X error code | numbers from Scweet |
| `graphql.py` | Layer 1 client: SearchTimeline, UserByScreenName, UserTweets, TweetDetail | new, twscrape parser as reference |
| `parse.py` | GraphQL timeline -> Argus post/user model | new |

Glue: `server.py` registers `x_search`; `fetch/core.py` routes x.com / twitter.com URLs to the X
module the same way it routes StackExchange and npm (adapter returns `render_path: "api"`).

### Layer 0 (always on, no account)

1. `x_search` without an account: SearXNG query `site:x.com <query>` (plus `site:twitter.com`),
   keep only `/<user>/status/<id>` URLs, dedupe by id.
2. Enrich each id through FxTwitter (bounded concurrency 4). Failure of one id drops that id only.
3. Result `source: "index"`, `degraded: true`, `degraded_reason: "index_only"` when the caller
   asked for `latest`.

### Layer 1 (opt-in, one account)

Active only when `ARGUS_X_AUTH_TOKEN` is set (with `ARGUS_X_CT0`, or `ct0` bootstrapped).

1. `manifest.get(op)` returns `(queryId, features)`; cache 6 h on the argus-data volume; a 404
   or error 336 forces one refresh and one retry.
2. `limiter.acquire()` blocks or fails fast (`x_rate_limited`, retryable, with `retry_after`).
3. Request: bearer = public web token, headers `x-csrf-token`, `x-client-transaction-id`
   (rebuilt every 6 h and once on 404), cookie jar scoped to `.x.com`.
4. Response: rotated `ct0` persisted; X error codes mapped through the cooldown table.
5. Egress: the server IP directly, never WARP (one stable IP per account).

`x_search` uses Layer 1 when available and falls back to Layer 0 on `x_account_*` errors
(`degraded: true`, reason names the account state). `read` of a status URL uses TweetDetail
(post + thread + first replies page) when Layer 1 is up, else FxTwitter.

HTTP stack: existing httpx client first. A spike measures whether Cloudflare blocks httpx on
the four operations; `curl_cffi` is added only if it does (new dependency, pinned, reviewed).

## Tool contract

### `x_search(query, mode="top", limit=20, since=null, until=null)`

- **in:** `query` (X search syntax, e.g. `from:espressif lang:en`), `mode` in {top, latest},
  `limit` 1-50, `since`/`until` `YYYY-MM-DD` (validated ASCII digits, real dates).
- **out:** `{query, mode, source: "graphql|index", results: [post], count, degraded,
  degraded_reason, from_cache}`.
- **post:** `{id, url, created_at, text, lang, author: {handle, name, verified, followers},
  metrics: {replies, reposts, likes, quotes, views, bookmarks}, media: [{type, url}],
  quoted: post|null, in_reply_to: id|null, source}`. `text` has t.co links expanded.
- **cache:** latest 300 s, top 1800 s (key includes mode and dates).
- **errors:** `x_rate_limited` (retryable), `x_account_locked`, `x_account_expired`,
  `x_schema_changed` (manifest refresh failed), `search_backend_down`, `no_results`.

### `read` on x.com / twitter.com URLs

- `/<user>/status/<id>`: one post (FxTwitter) or post + thread + replies (Layer 1), returned as
  `content` markdown plus `metadata.structured` = post model.
- `/<user>`: profile + latest posts (Layer 1 only; Layer 0 returns profile only).
- Everything else falls through to the normal fetch path.

Tool count: 20 -> 21.

## Safety limits and secrets

| Setting | Default | Why |
|---|---|---|
| `ARGUS_X_AUTH_TOKEN`, `ARGUS_X_CT0` | unset (Layer 1 off) | Coolify secret, never in repo, never logged |
| `ARGUS_X_MAX_PER_WINDOW` | 50 requests / 900 s | Scweet's measured value |
| `ARGUS_X_DAILY_REQUESTS` | 300 | Stays far below the 1M posts/24 h clause |
| min gap | 1 s + 0-2 s jitter | No burst signature |
| `ARGUS_X_FXTWITTER_URL` | `https://api.fxtwitter.com` | Allows a self-hosted FxEmbed later |

- Rotated `ct0` is written to `~/.argus/x_session.json` (mode 600). The auth token itself stays
  only in env.
- A test asserts that no log line or error `detail` ever contains the cookie values.
- Hosts contacted: x.com, api.x.com, abs.twimg.com, api.fxtwitter.com. All go through the
  existing SSRF-validated client.
- Everything returned is untrusted third-party content (same rule as every Argus tool).

## Observability

- `/health`: `x_account: off|ok|rate_limited|locked|expired`, `x_manifest_age_s`.
- `/metrics`: `argus_pipeline_stage_total{stage="x.graphql|x.index|x.fxtwitter|x.manifest_refresh|x.cooldown"}`.
- Reachability probe adds `api.fxtwitter.com`.

## Testing

- Offline: xclid against fixed test vectors; manifest regex against saved bundle snippets;
  limiter (window, daily cap, cooldown per code) with a fake clock; parser against recorded
  timelines (our own captures, scrubbed, plus twscrape MIT fixtures with attribution);
  index filter and FxTwitter mapping; secret-redaction test.
- Live: `x_search` added to `mcp_smoke.py` (Layer 0 always; Layer 1 when the account is set).
- Gates unchanged: SSRF 100% coverage, ruff clean, offline suite green, live 21/21.

## Rollout

| Phase | Ships | Exit gate |
|---|---|---|
| A | Layer 0: `x_search` (index) + `read` adapter via FxTwitter | Live smoke OK, no account needed |
| B | Layer 1 code + spike (httpx vs curl_cffi) | Offline tests green; spike result recorded |
| C | Layer 1 live with the owner's account after 1-2 weeks of normal use | 7 days in production with no lock, `/health` ok |

## Risks

| Risk | Mitigation |
|---|---|
| Account suspended | Single aged account, strict pacing, Layer 0 fallback keeps the tool working |
| X changes transaction-id algorithm | Health shows `x_schema_changed`; fix is one vendored file; upstream twscrape commits watched |
| FxTwitter goes down or rate-limits | Fall back to Argus `read` of the x.com page; self-host FxEmbed (MIT) if needed |
| ToS breach claim | Volumes stay orders of magnitude below the damages clause; no resale; owner accepts the account risk |

## Attribution

`src/argus/x/xclid.py` keeps the MIT notices of twscrape (c) 2023 vladkens and
iSarabjitDhiman/XClientTransaction. Ideas taken from Scweet (c) 2020 Altimis Teams and Tweety
(c) 2021 Tayyab Kharl are credited in the module docstrings and CHANGELOG.
