<div align="center">

# CHANGELOG - Argus

<img src="https://img.shields.io/badge/format-Keep_a_Changelog-2dd4bf?style=flat-square" alt="Keep a Changelog"/>
<img src="https://img.shields.io/badge/tools-20-22c55e?style=flat-square" alt="20 tools"/>
<img src="https://img.shields.io/badge/tests-700+-3fb950?style=flat-square" alt="700+ tests"/>
<img src="https://img.shields.io/badge/status-LIVE-16a34a?style=flat-square" alt="live"/>
<img src="https://img.shields.io/badge/created-2026--06--24-0ea5e9?style=flat-square" alt="created"/>

</div>

All notable changes, in [Keep a Changelog](https://keepachangelog.com/) style. Dates are absolute (`YYYY-MM-DD`). Argus went from research to a 20-tool, security-audited, benchmarked, **publicly-deployed** MCP server in two intensive days (2026-06-24 build, 2026-06-25 deploy + tuning); early entries are grouped by build phase rather than calendar day.

---

## [0.4.26] - 2026-10-04 - an alert when WARP breaks

A broken WARP was silent by design: Argus stays up and the listed hosts quietly go back
to the VPS IP, which they block. The only trace was `fetch.egress_proxy_fail` on a
loopback-only metrics page nobody watches.

### Added

- `/health` reports `"egress": true|false` when `ARGUS_EGRESS_PROXY` is set. The probe
  fetches Cloudflare's trace through the proxy and needs `warp=on`, so it also catches a
  WARP registration that stopped working (the image is a pinned third-party build). One
  probe per 60 s however often `/health` is hit; a failure logs a warning.
- The status and the HTTP code ignore it: an unhealthy Argus would be dropped from
  Traefik's routing over an optional dependency.
- `.github/workflows/uptime.yml` (every 30 min, already e-mails the owner on failure) now
  fails on `"egress":false`.

---

## [0.4.25] - 2026-10-04 - WARP egress, closing the gaps

Three validation passes over 0.4.23-0.4.24: the whole suite inside the production image
(976 passed on 0.4.24, including the 3 real-Chromium tests; 994 on this release before
the second review), live end-to-end and attack runs, and two independent reviews (of
`11abf02..HEAD`, then of this release). Everything they found is fixed here.

### Fixed

- **Malformed `ARGUS_EGRESS_PROXY`** (review): it escaped as a raw `ValueError` on every
  read of a listed host and echoed the value, credentials included. It is now validated
  once at startup (`http://host:port`, no userinfo) and switched off with a warning that
  does not print it.
- **Hung WARP in the httpx tier** (review): a proxy that accepts the TCP connection and never
  answers raised `ReadTimeout`, which skipped the direct fallback. Every handshake failure
  (unreachable, refused, reset, hung) is now `ProxyError`, the proxied hop has an 8 s
  connect bound, and the fallback catches exactly that.
- **Cookies** (review): dropping all cookies on the proxied client would loop a
  set-cookie-then-302 wall. Cookies now live in a per-call jar on both paths, and every
  SSRF-safe client keeps none, so one caller's site cookies never reach the next (this
  also closes the older cross-caller leak through the shared direct client).
- The proxied client is cached per event loop (a pooled client belongs to its loop).
- `warp` is no longer in `depends_on`: Argus goes direct when WARP is down, so a failed
  WARP image must not keep Argus from starting. `ARGUS_EGRESS_PROXY_HOSTS` empty in
  compose now means the default list in `config.py` (one source of truth).
- **Browser refusals counted by cause:** `DNSError` subclasses `SSRFError`, so a dead
  tracker domain was counted as `fetch.browser_ssrf_blocked`. The stage is now
  `fetch.browser_ssrf_blocked` or `fetch.browser_dns_failed`, and the refused request line
  is logged.

- From the second review: proxy config and `parse_proxy` now agree (empty userinfo
  `http://:@warp:9091` and port 0 are rejected by both); a TLS failure through the tunnel
  (`ConnectError`/`ConnectTimeout` on the proxied hop) also falls back to direct; the
  proxied client is cached per (loop, proxy); the refusal log drops the query string;
  the whole Wayback step has a 20 s deadline (`ARCHIVE_DEADLINE`, stage
  `fetch.archive_fail_deadline`, no cool-down).
- **INFO logs never reached `docker logs`** (found by the live pass, older than this
  feature): uvicorn leaves the root logger without a handler, so every `argus.*` INFO
  record (the fallback ladder, egress refusals) was dropped and only WARNING+ came out
  through `logging.lastResort`, although `ARGUS_LOG_LEVEL` is documented. The package
  logger now has its own stderr handler.

### Added

- **Wayback CDX fallback.** `/wayback/available` answered "no snapshot" for 4 of 7 URLs
  that have one (measured through WARP); the CDX API found 3 of those 4. It is asked only
  after an empty answer, because it is slow (1-16 s) and timed out on 3 of 7, and its
  misses do not feed the archive cool-down. Stages `fetch.archive_cdx_ok` /
  `fetch.archive_cdx_fail`.

### Docs and tests

- `deploy/README.md` (containers, env, the WARP note), `deploy/argus.env.example`,
  `README.md` (stack, test count, the S2 key line that was stale since 2026-09-19),
  `docs/00-DESIGN.md` (request path, the real fallback ladder), `docs/02-ROADMAP.md`,
  `AGENTS.md`, the `fetch/crawl.py` docstring (stale since 0.4.20).
- New tests: end-to-end through a real CONNECT server (tunnel, refuse, hang, dead), per-hop
  routing back to direct, TLS failure through the tunnel, the cookie wall, no cookie across
  hosts, the archive deadline, the 8 KiB cap over several reads, config
  validation, and a conftest default that keeps a developer's `ARGUS_EGRESS_PROXY` out of
  the suite. `config`, `fetch/static.py`, `fetch/fallback.py`, `security/ssrf.py` and
  `security/egress.py` are all at 100% line and branch coverage.

---

## [0.4.24] - 2026-10-04 - the browser tier uses WARP too

### Added

- Chromium's loopback egress proxy (`security/egress.py`) now dials listed hosts through
  `ARGUS_EGRESS_PROXY` with the same `CONNECT <validated-ip>` tunnel as the httpx tier
  (`ssrf.connect_tunnel`, shared). It still resolves and validates every connection
  first; a down, refusing or hung (8 s) WARP falls back to the direct path. Stage counters
  `fetch.browser_egress_proxy` and `fetch.browser_egress_proxy_fail`.
- Host matching lives in one place, `config.egress_proxy_for`, used by both tiers.

### Verified on the image built on the VPS

- Rendered through WARP, no challenge page: Reuters (579 KB), WSJ (889 KB), FXStreet
  (1 MB); Cloudflare trace from Chromium shows `warp=on`.
- With `searxng`, `warp`, `localtest.me` and `httpbin.org` added to the proxied list,
  direct navigation to internal names was refused by the pre-navigation guard and public
  302s to `searxng:8080` / `warp:9091` were refused by the egress proxy.

---

## [0.4.23] - 2026-10-04 - WARP egress for hosts that block the VPS IP

Idea from a "free proxy pool" post (Cloudflare WARP wrapped as SOCKS, rotated on rate
limits). Measured from the VPS before building anything, with three WARP accounts:

| Target | VPS IP | WARP |
|---|---|---|
| Wayback availability API and snapshots | 429 | 200, real snapshot |
| Reuters | 401 (DataDome) | 200, full page |
| WSJ | 401 | 200 |
| FXStreet | 403 | 200 |
| Semantic Scholar | 1 of 8 answered | 1 of 24 answered |
| DuckDuckGo, Qwant, Mojeek, Google Scholar, StackOverflow HTML, Reddit, Bloomberg, Medium | blocked | still blocked |

All three accounts left through the **same IPv4** (`104.28.222.43`, SIN); only IPv6
differed, and the StackExchange quota counted them as one. So there is no pool to rotate:
WARP is one extra, cleaner exit, and only for the hosts that improved.

### Added

- `warp` compose service (digest-pinned `ghcr.io/mon-ius/docker-warp-socks`, compose
  network only, no published port) and `ARGUS_EGRESS_PROXY` (default `http://warp:9091`
  in compose, empty = off) plus `ARGUS_EGRESS_PROXY_HOSTS` (default
  `archive.org,reuters.com,wsj.com,fxstreet.com`, subdomains included).
- `fetch/static.py` picks the client per redirect hop, so every caller of `fetch_static`
  and `fetch_bytes` (read, the Wayback fallback, PDFs) gets it. Stage counter
  `fetch.egress_proxy`.

### Security

- The SSRF pinning is unchanged: `_PinnedBackend` resolves and validates the target, then
  sends `CONNECT <validated-ip>:<port>` to the proxy. The proxy never sees a hostname, so it
  cannot resolve to a private address; TLS runs end to end over the tunnel with the real
  SNI. Refusals, hang-ups and oversized replies close the stream (100% branch coverage).
- Attacked on the real image built on the VPS: with `localtest.me`, `nip.io`, `warp`,
  `searxng`, `127.0.0.1` and `httpbin.org` all listed as proxied hosts, every private
  target and every public 302 to `searxng`/`127.0.0.1` was refused (9 of 9).
- From the independent review: the proxied client keeps no cookies (it is shared by every
  caller), a down or refusing proxy falls back to the direct path for that hop, the tunnel
  prefers a validated IPv4 address (the WARP exit is IPv4), bytes after the CONNECT reply
  are refused, `via_proxy` must be `http://host:port`, and a test pins NAT64
  (`64:ff9b::/96`) as blocked. The browser tier still exits from the VPS IP.

---

## [0.4.22] - 2026-10-02 - the P2 backlog

The remaining gap-audit items, each measured or reviewed before it went in. An
independent review of the first draft found two high-severity issues (below), both fixed
before release.

### Fixed

- **DNS failures are `dns_failed`, not `ssrf_blocked`.** Only a transient resolver answer
  (EAI_AGAIN) is retried, once; NXDOMAIN and timeouts are not, so no second uncancellable
  resolver thread is stranded. Per-attempt timeout 5 -> 8 s. A blocked address still
  refuses at once and never enters a fallback.
- **Regex denial of service in the markdown passes** (review finding): 5000 backticks
  took 7.7 s and 100k brackets 55 s, holding the GIL. Lines past 2000 chars now skip the
  per-line patterns and link patterns are bounded; both cases run in under 1 s.
- **Block detection** uses crawl4ai's antibot detector (DataDome, PerimeterX, Incapsula,
  Kasada, Akamai fingerprints), but on a 2xx a hit only counts when the page is thin, so an
  article quoting "Pardon Our Interruption" or an AWS "Access Denied" doc is content.
- **Static 5xx** (500/502/504, Cloudflare 520-526) goes down the fallback ladder instead
  of being extracted as content.
- PDF page cap (`ARGUS_PDF_MAX_PAGES`, default 300) also applies to explicit `pages`
  ranges and to the Docling slice, so `pages="1-5000"` cannot pin the PDF worker.

### Added

- **Negative cache:** a plain read that hit an anti-bot wall on every rung fails fast for
  10 minutes. Only `blocked_by_antibot` is remembered (not timeouts), and browser requests
  (`scrape`, `screenshot`, actions, wait_for) always get a fresh attempt (review finding:
  a failed read used to refuse the `scrape` an agent tries next).
- **Wayback cool-down:** a 429/403/503 from the archive switches the step off for 30
  minutes (it answers 429 on every endpoint from the VPS IP), each request capped at 10 s,
  raw `id_` snapshots, and the failure reason recorded as a stage.
- **Search:** list queries fan out (up to 4, merged with reciprocal rank fusion) under a
  process-wide limit of 8 backend searches; SearXNG `request_timeout` 6 -> 3 s (measured:
  responsive engines answer in 0.01-1.1 s, suspended ones instantly); escalating engine
  cooldown (120 s doubling to 1 h, reset on an answer).
- **Research:** at most 2 sources per host in the first pass, a per-call random fence tag
  in answer mode, and MCP progress notifications per source (fire-and-forget, so a slow
  client cannot delay or cancel a source).
- **Extraction:** precision and balanced passes both run; balanced replaces precision only
  when it recovers over 1.3x the text. On WCXB (50 pages per type) that moved overall F1
  0.684 -> 0.711 (forum 0.638 -> 0.713, article 0.930 -> 0.944) while keeping precision's
  markdown structure on small pages (balanced alone scored 0.715 but flattened them).
  Code fences keep their language, one-line `<pre>` stays a block, tables without `<th>`
  get a separator row, `text` derives from the same extraction as markdown, and
  regulation pages expose `metadata.pdf_links`.
- **MCP surface:** JSON-schema enums for `format`, `category`, `time_range`, `safesearch`,
  `mode` and `report_type`; screenshots arrive as an `image/png` content block; the bearer
  token is compared in constant time; INSTRUCTIONS cut from 1659 to 799 chars.
- **`benchmark/run_wcxb.py`**: offline extraction benchmark on the 2008-page WCXB set
  (full dev split: 0.799 word-F1 before this release; plain trafilatura 0.813).
- **CI `browser` job** runs the real-Chromium and real-model tests, which were deselected
  everywhere until now.

### Changed (breaking)

- `screenshot` results no longer carry the base64 PNG in `structuredContent`; over MCP the
  image is a separate content block. Callers of the Python function still get the dict.

## [0.4.21] - 2026-10-02 - the backlog, decided by measurement

The P1 items from the 0.4.20 gap audit. Each one was measured from the VPS IP or on an
eval set before it went in, and one was dropped because the numbers said so.

### Added

- **StackExchange API fallback** (`fetch/adapters.py`). stackoverflow.com question pages
  answer 403 to the VPS IP while `api.stackexchange.com` answers 200, so after a static
  failure on a question URL (stackoverflow, superuser, serverfault, askubuntu,
  `*.stackexchange.com`) the ladder reads the question and top answers from the API before
  trying the browser. `render_path: "api"`, stages `fetch.adapter_ok` / `fetch.adapter_fail`.
- **OpenAlex in `scholar_search`**, between Semantic Scholar and CrossRef. S2 still 429s
  about half the time, so its retry chain went from 3 retries (7 s of sleep) to 1. Keyless
  OpenAlex is about 100 searches/day; an optional free key `ARGUS_OPENALEX_API_KEY` (bearer
  header, never in URLs or logs) raises that to about 1000.
- **Chromium self-healing.** When Playwright reports the shared browser closed (crash, OOM
  kill), the pool relaunches it once under a lock and retries the render; `/health`
  reports a dead browser instead of `ok`.

### Changed

- **Multilingual embedder.** `paraphrase-multilingual-MiniLM-L12-v2` replaces
  `bge-small-en-v1.5`. On `benchmark/semantic_id.yaml` (32 queries, 22 Indonesian, hard
  negatives) Indonesian AUC went 0.894 -> 0.960 and nDCG@3 0.850 -> 0.933; English AUC
  1.000 -> 0.956. bge scored Indonesian junk 0.64-0.86, so `_SEM_FLOOR` 0.3 rejected 0% of
  it; the floors are recalibrated to 0.40 / 0.60. Cost: about +400 MB RSS (container
  ~1.2 GB measured) and a 470 MB model baked into the image.
- **The container runs as uid 10001**, not root. Browsers and the model live under `/opt`;
  data moved to a new `argus-data` volume at `/home/argus/.argus` (the root-owned
  `argus-cache` volume is no longer mounted; its 13 MB cache rebuilds on its own).

### Measured and not adopted

- **Patchright** (`UndetectedAdapter`): 4/12 blocked sites vs 5/12 for the current
  playwright-stealth tier, the same sites blocked in 0.3 s - before any JavaScript runs,
  so the decision is made on the IP, not the fingerprint. Not worth +200 MB of image.
  For the same reason the stale `Chrome/116` user agent was left alone.

## [0.4.20] - 2026-10-02 - the gap audit: a public server has to act like one

A six-dimension audit (fetch, extraction, search, MCP surface, security, ops) against the
October 2026 landscape, the 13-day live logs, and measurements taken from the VPS IP. The
full write-up, including what was measured and rejected, is
[`docs/qa/2026-10-02-gap-audit.md`](docs/qa/2026-10-02-gap-audit.md).

### Security

- **Browser-tier SSRF closed.** Only the seed URL used to be validated, so Chromium
  followed redirects, subresources and page JavaScript to internal hosts; a public 302 to
  the internal SearXNG service returned its config through `scrape`. Chromium (normal,
  stealth and `crawl`) now runs with `--proxy-server` pointing at a loopback egress proxy
  (`security/egress.py`, `--proxy-bypass-list=<-loopback>`): every connection - each
  redirect hop, subresource, page `fetch()`, WebSocket - is validated by the same SSRF gate
  and dialled to the validated IP, so Chromium never resolves DNS and rebinding is closed
  too. A Playwright route handler was tried first and dropped: it only sees the first
  request of a redirect chain, which only showed up when the image was attacked live.
  Verified in the built image: 302s to `127.0.0.1`, `localhost` and the metadata IP, and a
  page `fetch()` to loopback, are all refused; ordinary and JS-heavy pages still render.
- **IP pinning moved to connect time.** The pin used to rewrite the URL host to the IP, so
  the pool shared one TLS socket across every hostname behind a CDN IP (wrong-host
  requests, false 403s, second host's certificate never checked) and `final_url` showed
  the IP. An httpcore network backend now pins each new connection; URLs keep hostnames.
- Hashed `requirements.lock` constrained to the versions running in production; base
  image and SearXNG pinned by digest. CI installs the same lock.
- `research.max_sources` capped at 10, `batch_read` concurrency at 16, watches at 50;
  `list_watches` shows only the webhook's scheme and host; public `/health` returns status
  only; container gets `mem_limit: 3g` and `no-new-privileges`.

### Fixed

- Every `err()` result now reaches clients with `isError: true` (127 failures in 13 days
  looked like successes).
- CPU-bound extraction, PDF parsing and embeddings ran on the single event loop, which is
  why `research` p99 (223 s) overran its own 120 s cap. They run in threads now; PDFs share
  one worker because PyMuPDF is not thread-safe.
- `_dedup_blocks` was O(n^3): 4000 blocks 16.6 s -> 0.003 s, identical output.
- Metadata date search took 31 s on a large listing and invented dates from copyright
  footers; `extensive=False` brings it to 0.7 s.
- `language=en` was sent on every search, Indonesian included; SearXNG's `auto` applies.
- `include_domains` now reaches SearXNG as `site:` terms instead of filtering afterwards.
- `research` returns a partial bundle (`degraded_reason: budget_exhausted`) at 90% of its
  budget instead of discarding fetched sources; each source gets `min(50, timeout/2)` s.
- `read_pdf(mode="tables")` found no tables under pymupdf4llm's layout mode; fixed with
  `find_tables(use_layout=False)`. pymupdf messages no longer go to stdout (the stdio
  transport's JSON-RPC stream).
- `scrape` no longer returns raw page HTML labelled as markdown; retries no longer reuse
  just-benched engines; scholar filters go to the backends; the router reads Indonesian.

### Added

- Tool annotations on all 20 tools, `anthropic/maxResultSizeChars` 500k on the content
  tools, `stateless_http` (redeploys no longer drop sessions), one version source.
- Extraction: JSON-LD Product/Offer in `metadata.structured`, `metadata.lang`, a balanced
  pass when the precision result is thin, `metadata.pages_without_text` for PDFs.
- `/metrics`: cumulative `_count`/`_sum`, `cache.hit`/`cache.miss`,
  `fetch.browser_ssrf_blocked`. GitHub Actions: offline suite on push, `/health` every
  30 minutes (a failure e-mails the owner).

### Measured and not adopted

- `curl_cffi` recovered 2 of 14 blocked sites from the VPS IP, both already recovered by
  the browser tier; IP reputation, not the TLS fingerprint, is what blocks us.
- Wayback answers 429 on every endpoint from this IP (0 of 61 archive fallbacks worked);
  Common Crawl's index timed out at 10 s. Neither is a live fallback here.

## [0.4.19] - 2026-10-02 - what 13 days of live logs said

A pass over the container's log and `/metrics` from 2026-09-19 to 2026-10-02 (1276 tool
calls, about 10% structured errors) turned up the fixes below.

### Fixed

- **2.7 GB server RSS.** Two anonymous ~1 GB mappings in the uvicorn process were the
  ONNX Runtime CPU arena: fastembed embeds with `batch_size=256` by default and the arena
  keeps its peak forever. `semantic.embed` now passes `batch_size=8`. Measured locally on
  48 x 512-token docs: batch 48 retained +1005 MB, batch 8 retained +263 MB, same speed.
- **`github_search(mode="repos")` failed 17 times.** `mode` and `order` are now `Literal`
  enums, so the JSON schema lists the allowed values and clients pick a valid one.
- **`read_pdf(url=...)` failed argument validation.** Agents send `url` as they do for
  every other tool; it is now an alias for `url_or_path`.
- **`read_pdf(mode="quality")` raised `No module named 'docling'` on the VPS.** The image
  leaves out the `pdf-quality` extra on purpose (torch, GBs of RAM on a 7 GB box), so the
  mode now falls back to `tables` and says so in `metadata.quality_fallback`.
- **Log noise.** crawl4ai's per-URL console lines and source dumps came back on every run
  because `CrawlerRunConfig(verbose=True)` is the default; both run configs now pass
  `verbose=False`. readability's traceback on every empty (blocked) page is silenced; the
  caller already catches it.

### Added

- `argus_process_resident_bytes` gauge on `/metrics`, so memory growth is visible
  without SSH.
- `ARGUS_GITHUB_TOKEN` declared in `docker-compose.yml` so a token set in the panel
  reaches the container. 15 of 71 `github_search` calls hit the anonymous rate limit.

### Checked, no change

- `fetch.fallback_stealth_ok` = `fallback_stealth_fail` = `fallback_exhausted` = 61 looked
  like a counter bug. It is not: every stealth failure also exhausts (Wayback recovered
  nothing), and 61 + 61 + 4 = 126 `static_fail`.
- Anti-bot failures (monotaro, scribd, indotrading, biggo.id refusing the VPS IP) remain;
  only a residential proxy would change that, and it stays declined.

## [0.4.18] - 2026-09-19 - the S2 key arrives, and one attempt is still not enough

The Semantic Scholar key requested on 2026-09-18 was approved and is now set on the
Easypanel service (`ARGUS_S2_API_KEY`, never committed). Measuring it from the container
turned the obvious win into a smaller one.

### Added

- **`ARGUS_S2_API_KEY` live on the VPS.** Set through `updateComposeEnv`
  (`createDotEnv: true`) plus `deployComposeService`; `docker-compose.yml` already
  declared the variable, so it reaches the container rather than stopping at `.env`.
  Verified inside the container: a bogus key answers 403, ours answers 200, so the
  header is being honoured.

### Changed

- **S2 429 retry budget 2 -> 3, backoff base 0.5 s -> 1.0 s** (`_S2_BACKOFF_BASE`, new
  constant, now the single source of truth the tests read). The key's documented ceiling
  is 1 request per second, so a 0.5 s first retry was always going to be refused.
  Measured from the container: 1 of 10 keyed requests succeeded at 1.2 s spacing, and
  roughly half succeeded even at 6 s spacing, which is far below the advertised limit.
  With four attempts at 1 s / 2 s / 4 s the probability of falling through to CrossRef
  drops to about 6%, at a worst case of 7 s added before the fallback (the tool's own
  timeout is 120 s).

### Notes

- The key raises S2's hit rate, it does not make S2 reliable. CrossRef stays the
  fallback and `source` in the response still tells which backend answered.
- F1 in `CLAUDE.md` is closed. No repo file carries the key.

---

## [0.4.17] - 2026-09-18 - the general fan-out doubles, and Google was never blocked

Trimming the dead engines left `general` on three, which is no slack at all when one of
them throttles. So the other side of the question got measured too: of the 50
general-category engines this image ships disabled, which actually answer from this host?

### Added

- **`google`, `duckduckgo web` and `yandex` enabled.** All three answered every probe:
  `google` 10 results/query at 0.2 s - the real engine, not the CSE wrapper, and never
  actually blocked here despite years of assuming it was; `duckduckgo web` 10 at 0.7 s,
  the same name split that makes `duckduckgo news` work while plain `duckduckgo` is
  CAPTCHA-blocked; `yandex` 15 at 1.0 s. The general fan-out goes from three engines to
  six, four of them independent crawls.
- Measured usable and deliberately left off: `zapmeta` (9/query), `resulthunter` (20),
  `reloado` (16), `yahoo` (7) - aggregators or bing re-servers, so they add duplicates
  rather than a new crawl. Recorded in `settings.yml` for when the fan-out needs padding.
- Dead from this IP, so nobody re-probes them: `seznam`, `mwmbl`, `yacy` (timeout);
  `yep`, `privacywall`, `fireball`, `searchmysite`, `fastbot`, `tusksearch` (access
  denied); `crowdview`, `encyclosearch`, `wiby` (nothing).

### Fixed

- **`check_engines.py` defaulted to a list that could not pass.** Run bare it asked for
  the disabled proxy-bound engines and always exited non-zero, which is useless for the
  post-deploy gate it is meant to be. It now defaults to the live fan-out.
- Stale comment in `search.py` arguing the fan-out from a DuckDuckGo measurement that
  stopped being true when that engine got blocked.

---

## [0.4.16] - 2026-09-18 - the news, science and it categories, measured

`general` was only a third of the picture: `smart_search` and `search(category=...)` route
to `news`, `science` and `it`, and those send no `engines=` at all, so SearXNG's whole
default set for the category runs. Measured every enabled engine in all three, twice, with
on-topic queries (a zero on an off-topic query proves nothing about a narrow index).

### Fixed

- **Eight dead engines disabled.** Each returned nothing on every probe while still costing
  a sub-request per search: `reuters` (HTTP error), `pubmed` (the engine itself crashes),
  `openairepublications` and `openairedatasets` (5 s timeout each, the most expensive of
  the set), `gentoo` (HTTP connection error), `pypi` (no results for "httpx" or
  "pydantic"), `arch linux wiki` (none for "systemd" or "pacman"), and `google scholar`
  (access denied from this datacenter IP, so it points at the same `proxied` network as
  the other IP-blocked engines rather than being written off).
- **`science` was the worst hit**: five of its eight engines were dead, two of them
  burning a 5 s timeout on every query. What remains all answers: `arxiv`, `europepmc`,
  `semantic scholar`, `pdbe`.

### Verified

`news` is healthy - `brave.news` (41 results/query), `duckduckgo news` (26), `google news`
(10), `bing news` (9), `wikinews` (5). Worth knowing: `duckduckgo news` answers fine from
this IP even though plain `duckduckgo` is CAPTCHA-blocked; upstream treats them as
separate engines. `it` keeps `github` (30/query), `stackoverflow`, `docker hub`, `mdn`,
`askubuntu`, `superuser`, `hoogle` and `mankier`.

---

## [0.4.15] - 2026-09-18 - half the search fan-out did not exist

Chasing "is anything still stuck in SearXNG" found that two of the four configured
engines are not engines at all on this image, and that the checker could not see it.

### Fixed

- **`startpage` and `marginalia` do not exist in this SearXNG.** The image exposes 264
  engines and neither is among them; upstream dropped both. SearXNG silently discards an
  unknown name from `engines=` and, when nothing valid remains, answers from its default
  set instead - so asking for `startpage` alone returned 29 results attributed to `bing`
  and `brave`. The real general fan-out was `bing` + `brave` all along, plus whatever the
  default set adds. `ARGUS_SEARCH_ENGINES`, the in-code fallback and `settings.yml` now
  name only engines this instance actually has: `bing`, `brave`, `google cse`.
- **`scripts/check_engines.py` reported false passes.** It scored an engine on
  `len(results)` without checking who produced them, so a nonexistent engine passed on
  other engines' results - which is how the phantom pair survived a host move and a
  dedicated engine audit. It now counts only results attributed to the engine asked for,
  and says so explicitly: `unknown to this SearXNG; answered by bing, brave`.
- **`wikidata` disabled.** It timed out on nearly every query and was the only engine
  producing errors in the production log. It is a fact lookup, not a web index.

### Verified

Re-measured with the corrected checker: `bing` 10 results/query (0.3 s), `brave` 20
results/query (0.5 s), `google cse` the largest contributor when not in a rate-limit
suspension it entered during this audit's own probing. `mojeek`, `duckduckgo` and `qwant`
stay disabled behind the absent `proxy` service, as designed, and produced no errors in
2 h of production logs.

---

## [0.4.14] - 2026-09-18 - the Cloudflare edge, made to actually hold

The DNS record for `argus.gifariksuryo.xyz` was switched to Cloudflare-proxied. Measuring
what that changed found one thing it does not fix and two things it quietly broke.

### Fixed

- **The edge could be skipped entirely.** With the record proxied but the origin still
  answering on its own IP, `--resolve argus.gifariksuryo.xyz:443:43.134.17.144` reached
  Argus and returned 200, so Cloudflare was decoration. A new `argus-cloudflare-only`
  ipAllowList (the 22 published Cloudflare ranges) on the `/` router answers 403 to
  anything that did not come through the edge. It is scoped to that one router, so the
  hostnames that resolve directly to the origin by design keep working, and port 80 stays
  open for their ACME challenges.
- **Rate limiting counted every client as one.** Behind the edge each request arrives from
  a Cloudflare IP, so `argus-ratelimit` bucketed the owner and any attacker together, and
  400 requests from one source could starve the endpoint. It now keys on
  `CF-Connecting-IP`, which the gate above makes unspoofable. Re-measured: a 30-way
  parallel burst of 400 gets 249 rejections, the same profile as before.

### Verified, not changed

- **Proxied DNS does nothing for the blocked engines.** It is an inbound proxy; the egress
  IP is unchanged and `duckduckgo`, `qwant` and `mojeek` still return `proxy error`
  because the `proxy` compose service is not running. (The claim in this entry's first
  draft that `startpage` and `marginalia` answer 2/2 was wrong - see 0.4.15.)
- **Cloudflare's 100 s origin timeout never fires.** The MCP transport is
  `text/event-stream` with a FastMCP `: ping` every ~15 s, so nothing is ever idle: a
  180 s `crawl` returns `http=200 time=180.1s` through the edge.
- **Certificate renewal is unaffected** - Traefik's internal `acme-http` router carries
  none of these middlewares and Cloudflare passes the challenge path through.

---

## [0.4.13] - 2026-09-16 - Traefik middlewares, proxy path, engine checker

### Fixed

- **`/metrics` was reachable from the internet.** Under nginx it was loopback-only; the
  Easypanel migration routes the whole host to Argus, which published it - a regression
  introduced by 0.4.10 and caught by re-checking the endpoint. A path-scoped Traefik rule
  with an `ipAllowList` middleware restores the gate: `/metrics` now answers 403 from
  outside and 200 from the host, while `/health` and `/mcp` are untouched.

### Added

- **Rate limiting at the edge** (`argus-ratelimit`, 300 requests / 60 s, burst 100) on the
  public domain, replacing the brute-force protection the retired fail2ban jail provided.
  Verified: 150 sequential requests all pass, a 20-way parallel burst of 400 takes 248
  rejections.
- **`scripts/check_engines.py`** - probes each SearXNG engine from wherever it runs and
  prints results, median latency and the failure reason, exiting non-zero when an engine
  answers nothing. Engine availability depends on the host's IP, so it needs measuring
  after a host move or a proxy change rather than guessing.
- **A proxy path for the blocked engines.** `settings.yml` defines
  `outgoing.networks.proxied` and points `mojeek`, `duckduckgo` and `qwant` at it; the
  target is the compose service `proxy`, so no provider credential is ever committed.
  `docker-compose.proxy.yml` runs that service from `RESIDENTIAL_PROXY_URL`.

### Note

**Cloudflare WARP was measured as the free option and rejected.** It connects and gives a
Cloudflare consumer IP, but duckduckgo still returns a CAPTCHA, and qwant and mojeek still
return access denied - the WARP range is itself a known VPN. Only residential exit IPs
would change this, which means a paid provider.

Easypanel's free tier also declines metrics retention and log collection (`A license with
advanced monitoring support is required`), and accepts a notification channel with a 200
while storing nothing.

---

## [0.4.12] - 2026-09-15 - Silence the startup log noise

Every line the stack logged at startup was either a warning we caused or a warning we
could remove. All four are gone now, so a clean start is actually silent and the next
real warning stands out.

### Fixed

- **pymupdf deprecation.** `argus.extract.pdf` (and three test modules) imported the
  legacy `fitz` alias, which makes pymupdf print a deprecation warning on every boot.
  They import `pymupdf` directly now.
- **SearXNG could not load two engines.** `use_default_settings: true` pulls in the
  upstream onion engines (`ahmia`, `torch`), which need a tor proxy this instance does not
  have, so each start logged `can't register engine`. They are dropped explicitly via
  `use_default_settings.engines.remove`.
- **SearXNG missing `limiter.toml`.** The botdetection module reads that file at startup
  and warns when it is absent, even with `limiter: false`. `deploy/searxng/limiter.toml`
  now ships with the repo and is mounted alongside `settings.yml`. It follows the image's
  own `searx/limiter.toml` schema - the older `[real_ip]` section is deprecated and warns
  about each of its keys on every start.
- **SearXNG's proxy-header error.** SearXNG assumes it sits behind a reverse proxy and
  logs `X-Forwarded-For nor X-Real-IP header is set!` once per process when a request
  carries neither. Argus is the client, not a proxy, so the SearXNG request now names
  itself with `X-Real-IP`. SearXNG ignores the value (it honours proxy headers only from
  a trusted proxy), so nothing about routing or rate limiting changes.
- **Hugging Face fetch at runtime.** The rerank model was downloaded on first use, which
  printed a progress bar plus an unauthenticated-request warning, and made a cold start
  depend on the HF Hub being reachable. The Dockerfile bakes `BAAI/bge-small-en-v1.5`
  into the image instead, so the container starts offline and quiet.

### Changed

- **`ARGUS_SEARCH_ENGINES` picks the `general` fan-out.** Argus sends SearXNG an explicit
  engine list, so `disabled: true` in `settings.yml` never applied to its own queries -
  the blocked engines were still being called. Which engines answer is a property of the
  host's network, not of the code, so the list is now env-tunable: the VPS sets
  `bing,brave,startpage,marginalia`, and an unset variable keeps the built-in default for
  a normal network.
- **Disabled the same three engines in `settings.yml`.** Verified from the VPS
  with a fresh suspension state: mojeek never answers (25 s, no response at all),
  duckduckgo returns a CAPTCHA instantly, qwant returns access denied - all three are
  blocks on the datacenter IP range. Leaving them enabled cost a 6 s timeout on every
  search plus an ERROR line per query, and returned nothing. Re-enable them behind a
  residential proxy or on a different network. `bing`, `brave`, `startpage`,
  `marginalia`, `wikipedia` and `wikidata` answer normally and stay on.

### Note

Engine availability measured from the VPS on 2026-09-15: `bing` (0.2 s), `startpage`
(1.6 s) and `marginalia` (1.0 s) answer reliably; `brave` answers but SearXNG suspends it
with `too many requests` after a burst and it returns on its own after the configured
300 s. A `WARNING:searx.network.brave` line is therefore real signal, not a
misconfiguration - the only way to remove it would be to drop a working engine.

Two Easypanel gotchas worth remembering: `updateComposeEnv` silently turns off `.env`
generation unless `createDotEnv: true` is resent, and compose only passes variables the
service declares, so a key needs an `environment:` entry to reach the container. Both are
now in `deploy/README.md`.

SearXNG reads `settings.yml` and `limiter.toml` through a bind mount, and compose does
not recreate a container when a mounted file's content changes. A deploy that only edits
those files leaves the old config loaded - `docker restart argus_argus-searxng-1` after
one. Documented in `deploy/README.md`.

---

## [0.4.11] - 2026-09-15 - Clean compose for a shared host

Easypanel flagged three issues on the deployed stack, all of them the same class of
problem: settings that are fine for a machine running one stack, and wrong for a host
running many.

### Changed

- **Dropped `container_name` from both services.** A fixed name is global to the Docker
  daemon, so a second stack on the same host cannot start. Nothing needed the names:
  Argus reaches SearXNG at `http://searxng:8080`, which is the compose service alias, not
  the container name. Docker now names them `argus_argus-argus-1` / `argus_argus-searxng-1`.
- **Moved the published port into `docker-compose.local.yml`.** Publishing
  `127.0.0.1:8090` is what a workstation needs and exactly what the VPS does not - there
  Traefik reaches the container over the compose network, and a host port only invites a
  clash. Local runs become:
  `docker compose -f docker-compose.yml -f docker-compose.local.yml up -d`.

`getComposeIssues` now returns an empty list.

---

## [0.4.10] - 2026-09-15 - Easypanel deployment

The VPS now runs Argus under [Easypanel](https://easypanel.io/) instead of bare systemd,
so the box has a panel for the other SURIOTA services that still have to be rebuilt after
the old host died. Easypanel installs Docker + Swarm, runs Traefik on `:80`/`:443` with
Let's Encrypt, and serves its panel on `:3000`.

### Changed

- **Argus is an Easypanel Compose service**, built from this repo's own
  `docker-compose.yml` (project `argus`, service `argus`, source git `main`). Compose and
  not an App service on purpose: Chromium needs more than the default 64 MB of `/dev/shm`,
  and `shm_size` is a Compose setting Docker Swarm does not support.
- **Traefik terminates TLS** for `argus.gifariksuryo.xyz` and routes to the `argus`
  container on `:8090`. nginx, certbot's renewal timer, `argus.service` and
  `argus-update.timer` are disabled on the host but left installed as a rollback path.
- **Auto-deploy is a GitHub push webhook** into Easypanel's deploy URL, replacing the
  poll-every-5-min systemd timer. A broken `docker-compose.yml` or `Dockerfile` now breaks
  the deploy the same way broken Python would.
- **The nginx-log fail2ban jail is disabled** because nginx no longer sees traffic; the
  `sshd` jail still runs. Bearer auth is unaffected - Argus itself returns the 401.

### Added

- **`ARGUS_TOKEN` and `SEARXNG_SECRET` passthrough in `docker-compose.yml`.** Both come
  from the service env with defaults that leave the loopback-only local stack unchanged:
  no token locally (nothing is exposed off-host), and a real token on the VPS so `/mcp`
  stays 401 without a bearer. `SEARXNG_SECRET` overrides `server.secret_key`, so the
  deployed SearXNG never runs on the placeholder value committed to the repo.
- **`deploy/README.md`** leads with the Easypanel runbook (panel API calls, rollback to
  systemd) and keeps the systemd recipe as the panel-less alternative.

### Operations

- Verified end to end over the public endpoint: Let's Encrypt cert issued by Traefik,
  `/health` 200 with `browser: true`, `/mcp` 401 without a token, MCP handshake +
  `tools/list` returning all 20 tools, and a live `search` through SearXNG.
- **Panel hardening:** the panel is at `https://panel.gifariksuryo.xyz` behind a
  Traefik-issued Let's Encrypt cert and the GitHub webhook posts there, so its deploy
  token no longer travels in clear text. `setPanelDomain{"serveOnIp": false}` does not
  actually stop the panel answering on the raw IP, so `:3000` is dropped from the
  internet with iptables rules in `INPUT` and `DOCKER-USER`, persisted via
  `iptables-persistent`.

---

## [0.4.9] - 2026-09-15 - Re-provision on a new VPS, provision.sh fixes

The old SURIOTA VPS `103.172.172.29` (Hermes, SUVA, Argus) went down. Argus was
re-provisioned from scratch on `43.134.17.144` (Tencent, Ubuntu 24.04, 2 vCPU / 7 GB),
which was the first end-to-end run of `deploy/provision.sh` on a bare host since the
original deploy. It surfaced six defects; each is fixed below, so the script now takes a
fresh Ubuntu 24.04 box to a live Argus in one run.

### Fixed

- **Repo landed in the wrong directory.** Step 5 cloned to `/tmp/argus-temp` then
  `mv`-ed it onto `$ARGUS_HOME`, which already existed from Step 4, so the repo nested at
  `/opt/argus/argus-temp` and the next `git config` aborted the run. Clone straight into
  the target directory instead.
- **Install layout disagreed with the systemd unit.** `provision.sh` installed the repo
  and venv at `/opt/argus`, while `argus.service`, `argus-update.sh` and
  `argus-update.service` all point at `/opt/argus/app` - the service died with
  `status=203/EXEC`. Added `ARGUS_APP="$ARGUS_HOME/app"` and moved every repo-relative
  path onto it; the caches the unit grants write access to stay in `$ARGUS_HOME`.
- **`log_error` tripped `set -e`.** It ended with `return 1`, so every warning-only call
  site (certbot without DNS, a slow SearXNG, `/health` not up yet) killed the run.
  Warning sites now only print; the sites that must abort still `exit 1` themselves.
- **`crawl4ai.setup` is not a runnable module.** Step 9 called
  `python -m crawl4ai.setup`; use the `crawl4ai-setup` console script the package ships.
- **`playwright install --with-deps` as the service user.** The `--with-deps` half needs
  root and re-invokes `sudo` with no TTY, so browser installation failed outright. Install
  the system libraries as root, then the browser binaries as `$ARGUS_USER` so they land in
  that user's cache. Patchright's browser is installed best-effort.
- **fail2ban files were stale.** Step 15 copied a `deploy/fail2ban-argus.conf` that no
  longer exists and wrote its own weaker inline filter; it now installs the maintained
  `deploy/fail2ban/argus.jail.conf` + `argus-mcp.filter.conf`.

### Changed

- **`DOMAIN_PLACEHOLDER` is now actually applied.** The installed nginx site gets the
  domain rendered into it (`sed` on `/etc/nginx/sites-available/argus`); the repo copy
  keeps the placeholder so the working tree stays clean for the ff-only auto-update.
- **`ARGUS_REPO`** points at the real repository (`GifariKemal/argus-web-mcp`) instead of
  a placeholder URL.
- **Step 18 polls `/health`** for up to 30 s instead of asking once; startup takes a few
  seconds for the browser warm-up.

### Operations

- New host is SSH key-only (`PasswordAuthentication no`, `PermitRootLogin no`), user
  `ubuntu`, host key `SHA256:UqvgBndlBJf4+w147tG64yoCSDzbXL22HDsmjaB8Ugw`.
- Verified on the new host: `/health` 200 with `browser: true`, `/mcp` 401 without a
  bearer token, MCP handshake + `tools/list` returning all 20 tools, and live `search`
  (SearXNG) and `read` calls. `argus-update.timer` is enabled and polling.
- **Pending owner action:** the `argus.gifariksuryo.xyz` A record still points at the dead
  IP, so certbot cannot validate and nginx is serving a self-signed placeholder cert.
  After repointing DNS to `43.134.17.144`:
  `sudo certbot certonly --webroot -w /var/www/letsencrypt -d argus.gifariksuryo.xyz && sudo systemctl reload nginx`

---

## [0.4.8] - 2026-08-10 - Local Docker mode

Run the whole stack on a workstation with one command, so the MCP's lifetime equals the
Docker container's: `docker compose up -d` starts it, `docker compose down` stops it. The
VPS systemd deployment is unchanged.

### Added

- **`Dockerfile`** - self-contained image (python:3.12-slim, `.[semantic]` extra, Chromium
  via `playwright install --with-deps`), serving `uvicorn argus.server:app` on `:8090`.
- **`docker-compose.yml`** (repo root) - `argus` + `searxng` in one stack. Argus publishes
  `127.0.0.1:8090` only; SearXNG has no host port at all and is reachable solely over the
  compose network. `shm_size: 1gb` (Chromium), named `argus-cache` volume for `~/.argus`,
  `restart: unless-stopped`, and a `/health` healthcheck. No bearer token: nothing is
  exposed off-host.

### Changed

- **`ARGUS_SEARXNG_URL`** now overrides the SearXNG backend base URL (default unchanged at
  `http://127.0.0.1:8888`, so the systemd/VPS path is untouched). Compose sets it to
  `http://searxng:8080`.

### Verified

`/health` -> `{"status":"ok","browser":true}`; `search()` inside the container returns
results with `backend: http://searxng:8080`; Chromium renders a live page (tier `normal`);
`claude mcp list` -> `argus: http://127.0.0.1:8090/mcp - Connected`; ruff clean; the 191
search tests stay green.

---

## [0.4.7] - 2026-07-14 - Observability, compression

Make every fallback visible (so future tuning is data-driven) and shrink what goes
over the wire and onto disk. All additive; no behavior change to a healthy request.

### Added

- **Pipeline-stage observability.** Each fetch-ladder hop and search fallback now
  increments a stage counter exported at `/metrics` as `argus_pipeline_stage_total{stage=...}`
  (`fetch.static_ok`, `fetch.static_fail`, `fetch.fallback_stealth_ok/fail`,
  `fetch.fallback_archive_ok`, `fetch.fallback_exhausted`, `fetch.thin_escalate*`,
  `fetch.forced_browser`, `search.engine_benched`, `search.backend_failover`,
  `search.low_relevance`). Shows which fallback fires most - the durable signal for
  tuning the tiers without grepping logs.
- **Per-step logging.** The same hops log at INFO on any fallback/escalation and at
  DEBUG for the happy path (raise `ARGUS_LOG_LEVEL=DEBUG` for the full per-step trace).

### Changed

- **Cache blobs are gzip-compressed on disk.** Full-page content compresses ~5-10x, so
  the on-disk blob store stays small on the VPS. The read path auto-detects gzip vs a
  legacy plain blob, so existing cache entries keep working with no migration.
- **nginx gzip for tool responses.** `application/json` + text responses are compressed
  over the wire (engages only when the client sends `Accept-Encoding: gzip`; works with
  `proxy_buffering off`). Cuts bandwidth on the large full-content bundles.

### Pruned

- Repo-wide over-engineering audit: no dead code, hand-rolled stdlib, or unused config
  found in `src/argus` (prior rounds already trimmed it). No churn.

## [0.4.6] - 2026-07-14 - Live-log-driven resilience pass

Improvements from a 7-day production journald audit (search-engine throttling, timeout
long-tails, benign teardown noise). All additive; no behavior change to a healthy request.

### Added

- **Client-side per-engine cooldown (`search`).** When SearXNG reports an engine
  `unresponsive` (rate-limited/CAPTCHA on a datacenter IP), Argus benches it for a window
  (`ARGUS_ENGINE_COOLDOWN`, default 120s) so the next general fan-out stops requesting it and
  concentrates on engines that answer. Complements SearXNG's server-side `suspended_times`;
  cuts the dominant log signal (~1200 "engines unresponsive" events/7d) and wasted sub-requests.
  Safety floor: never benches below 2 fan-out engines.

### Fixed

- **`scrape` wall-clock now bounded by its own `timeout`.** The normal->stealth escalation ran
  two renders (each up to `timeout + grace`), so a scrape could take ~2x its configured timeout
  (observed p99 ~184s at a 90s setting). Wrapped in an outer `asyncio.timeout(timeout)` ->
  structured `fetch_failed` "scrape timed out", matching the `research`/`crawl` pattern.
- **Client-inflated `timeout` clamped to the server ceiling.** `timeout` is a tool parameter, so
  a caller could pass `timeout=900` and blow past the intended bound (a likely cause of the
  `research` p99 ~858s tail). The metrics middleware now clamps every tool's `timeout` arg to
  `TIMEOUTS[name]` before dispatch (a caller may request less, never more).

### Changed

- **Log hygiene.** A loop exception handler demotes known-benign async-teardown tracebacks
  (client-disconnect `ClosedResourceError`, browser `net::ERR_ABORTED` / detached-frame futures)
  to a single debug line. Real errors still pass through to the default handler untouched.

## [0.4.5] - 2026-07-02 - Round 10 final gap-scan

Final gap-scan over the deployed a802678/v0.4.4 tree, focused on code added during
the benchmark reset/search/PDF tuning pass.

### Fixed

- **`search()` preserves `backend_failover` degradation through category rescue.** A primary
  backend failure followed by low-relevance fallback results and a successful routed rescue could
  previously clear `degraded`, causing fallback results to look clean and become cacheable. Rescue
  now clears only pure `low_relevance`; failover stays surfaced and uncached. Rescue against an
  external fallback backend also uses the SSRF-safe client path.
- **`smart_search()` now obeys the tool error contract.** Invalid non-string/empty queries return
  `schema_invalid`, and unexpected internal exceptions return structured `search_backend_down`
  instead of leaking across the MCP boundary.
- **3-way merge now requires complete Codex coverage by default.** The active compare set is 40 IDs;
  `merge-3way` fails fast when Codex output files are missing unless `--allow-partial-codex` is
  passed explicitly. Active Codex output path is now `benchmark/codex_compare/`.

### Docs / Benchmark

- Synced `smart_search` docs/instructions with the `science` route and documented
  `search.rescued_category`.
- Removed stale trading seams/text from the active tool-surface benchmark.
- Corrected status/date/benchmark-gate drift in `CLAUDE.md`, `docs/02-ROADMAP.md`, and
  `benchmark/reports/RESULTS.md`.

### Tested

- `ruff check src tests benchmark`
- Offline suite: 767 passed, 8 deselected.
- SSRF gate: 45 passed, 100% line + branch coverage for `argus.security.ssrf`.
- Browser marker: 3 passed. Slow marker: 3 passed. Network marker: 2 passed.
- Tool-surface smoke: 19/19 active non-trading boundaries OK.

## [0.4.4] - 2026-07-02 - Benchmark scope reset + search/PDF tuning

Follow-up benchmark pass after the end-to-end Argus stress work. The active benchmark
surface is now non-trading by default, while runtime trading tools remain available and
tested separately.

### Changed

- **Benchmark scope reset:** removed active trading/MQL5 scenarios from `benchmark/scenarios.py`,
  `benchmark/testset.yaml`, `benchmark/quality_gold.yaml`, burst tests, and the deterministic
  tool-surface benchmark. Active search scenarios are now 160 non-trading queries across 8
  categories; `COMPARE_IDS` is now 40 IDs. Active extraction testset is 16 URL items + 8 search
  queries. Removed the stale `gold/longform-01.md` market/investment gold reference.
- **Added deterministic tool-surface benchmark** (`benchmark/run_tool_surface.py`) over 19 active
  non-trading server tool boundaries using local fixtures only. Run artifacts are ignored under
  `benchmark/_runs/`.
- **Search relevance tuning:** default SearXNG language now resolves to English unless overridden,
  semantic low-relevance guarding is stricter, science routing is explicit, and weak general
  searches can rescue into routed categories (`science`, `it`, `news`) before being marked degraded.
- **PDF/read latency tuning:** large text PDFs take a fast PyMuPDF text path, and static fetches cap
  timeout earlier when browser fallback is available.

### Tested

- `ruff check src tests benchmark`
- Offline suite: 763 passed, 8 deselected.
- SSRF gate: 45 passed, 100% line + branch coverage for `argus.security.ssrf`.
- Browser marker: 3 passed. Slow marker: 3 passed. Network marker: 2 passed.
- Benchmark smoke: tool-surface non-trading 19/19 OK; quality benchmark 2/2 items at
  `quality_f1=1.000`; live search smoke over 16 non-trading scenarios hit 100% success and 0%
  throttle, with `dev` still flagged for low overlap/degraded review.

## [0.4.3] - 2026-07-02 - Gap-scan round 9: convergence + doc sync

A 17-agent workflow (4 subsystem deep-dives + 2 comprehensive doc-drift audits + synthesis +
adversarial verify) scanned the post-0.4.2 tree. Convergence continues - 4 small code fixes + a
full documentation sync. Suite 746 -> 753 passed, ruff clean, coverage 94% held.

### Fixed

- **`search()` low_relevance guard no longer false-flags semantic rescues.** The guard recomputed
  pure-lexical overlap even on the hybrid path, so a legitimate paraphrase set (zero lexical overlap
  but high cosine - exactly what the hybrid blend rescues) was wrongly flagged `degraded=low_relevance`.
  `_rerank_hybrid` now tags each row's semantic relevance (transient, stripped before return) and the
  guard credits a row as relevant on lexical overlap OR `cosine >= _SEM_FLOOR`. Genuine junk (low
  cosine AND no lexical overlap) still flags. Lexical-only path unchanged.
- **`screenshot()` now surfaces `blocked_by_antibot` via `FetchError.code`** (round 7 converted read/
  scrape but missed screenshot; the old `"antibot" in str(e)` substring never matched the real
  `"...(anti-bot block)"` message, so the branch was dead).
- **`map_urls` clamps `max_urls`** at the trust boundary (`max(1, min(max_urls, 5000))`, like crawl/
  find_similar) - a negative value previously dropped the last URLs and misreported `truncated=True`.

### Tested

- Guard-credits-semantic-rescue + still-flags-low-cosine-junk (hybrid path); screenshot antibot code;
  map_urls clamp (low + high); bogus meta-charset -> utf-8 LookupError fallback.

### Docs / chore

- **Version lockstep**: `pyproject.toml` + `argus.__version__` bumped `0.1.0 -> 0.4.3` (they had drifted
  from the CHANGELOG/tag through every release).
- **README + AGENTS** test-count badges/text corrected `722 -> 753`.
- **ROADMAP** P4 log now records rounds 7, 8, 9 (was Round-6 only).
- **`deploy/argus.env.example`** documents the remaining real env vars: `ARGUS_S2_API_KEY` /
  `SEMANTIC_SCHOLAR_API_KEY` (scholar rate-limit) and `ARGUS_HEALTH_LATENCY_BUCKETS` (/metrics buffer).
- **TOOL-SPECS** timeout literals + env-var docs are now in sync with the code (audited end-to-end).

## [0.4.2] - 2026-07-02 - Gap-scan round 8: 10 long-tail fixes

A 27-agent workflow (8 subsystem deep-dives + 3 SOTA-research + synthesis + adversarial
verify-per-finding) scanned the post-0.4.1 tree. After two prior audits the remaining gaps
are lower-severity but real; all shipped with offline regression tests. Suite 735 -> 746
passed, ruff clean, coverage 94% held. (Two verified trading-only fixes were intentionally
dropped - the trading tools are not in use here; and an opt-in `max_chars` cap for read/scrape
was deferred as a feature, not a gap.)

### Fixed

- **read()/scrape()/batch_read() silently coerced an out-of-enum `format` to markdown** while
  echoing the bogus label (and wasting a fetch); batch_read would then KeyError on the err dict.
  Now reject with `schema_invalid`, consistent with the read_pdf/category guards.
- **search() silently ignored an out-of-enum `time_range`** (SearXNG returns all-time results
  for a non-`{day,week,month,year}` value). Now `schema_invalid`.
- **Static fast-path mojibake'd meta-only legacy encodings** - httpx defaults to utf-8 with no
  header charset, corrupting windows-1251/shift_jis/etc. pages irreversibly. Now decodes with the
  header charset when present, else sniffs a `<meta>`/`<?xml>`-declared charset before utf-8.
- **research() highlights ran outside try/except** - a runtime embedding failure turned a
  successful bundle into an uncaught MCP error. Now guarded (skip highlights + log).
- **batch_read had no crash isolation** - an unexpected exception in one `read()` sank the whole
  batch. Now `gather(return_exceptions=True)` + per-URL failure normalization.
- **map_site fetched robots.txt `Sitemap:` directives uncapped**, bypassing `_MAX_CHILD_SITEMAPS`.
  The robots-derived seed list is now capped too.
- **Corrupt-blob cache self-heal leaked the blob file** - it deleted the DB row but left the
  orphaned file on disk. Now unlinks the blob as well.

### Changed / docs

- **`ARGUS_LOG_LEVEL` is now wired** (sets the `argus` logger level at import) - it was documented
  but read nowhere. The two truly-dead knobs `ARGUS_REQUEST_TIMEOUT` / `ARGUS_BROWSER_TIMEOUT` are
  removed from the deploy docs (the real knobs are the per-tool `ARGUS_TIMEOUT_*`).
- **Documented the real, previously-undocumented env vars** in `deploy/argus.env.example`: JWT auth
  (`ARGUS_JWT_JWKS_URI`/`ISSUER`/`AUDIENCE`), `ARGUS_GITHUB_TOKEN`, `ARGUS_COURTESY_DELAY`,
  `ARGUS_MIN_CONTENT_WORDS`.
- **Fixed 5 stale timeout defaults in `docs/03-TOOL-SPECS.md`** to match `config.TIMEOUTS`, and added
  a `test_config` guard that fails on future doc/config timeout drift.

## [0.4.1] - 2026-07-02 - Gap-scan round 7: 8 verified fixes

A 30-agent workflow (9 subsystem deep-dives + 4 SOTA-research + synthesis + adversarial
verify-per-finding) scanned the post-0.4.0 tree; 8 findings survived review, all with an
offline before/after and all shipped with regression tests. Suite 722 -> 735 passed, ruff
clean, coverage 94% held.

### Fixed

- **Whole-body article duplication** - `_dedup_blocks` was adjacent-only, so trafilatura
  2.x's verbatim re-emit of the entire `<article>`/`<main>` body (a contiguous run repeated
  right after itself: `[Title, A, B, A, B]`) was never collapsed, doubling returned tokens
  on well-structured pages for `read`/`research`/`scrape`/`crawl`. Now run-aware (collapses
  the longest adjacent run-duplication; single-block repeat is the `L==1` case); genuine
  non-adjacent refrains are preserved.
- **`scholar_search(open_access=True)` returned no_results on the CrossRef fallback** -
  `_map_crossref` hardcoded `open_access_pdf=None`, dropping every result on the common
  anonymous-S2-429 path. Now maps the first `application/pdf` entry from CrossRef's `link`
  array (URL-guarded).
- **`research()` had no overall wall clock** - the `timeout` arg bounded only per-source
  fetches, so sequential backfill waves could run ~3x the stated budget. Wrapped in
  `asyncio.timeout(timeout)` (mirroring `crawl`) -> structured `fetch_failed` on overrun.
- **Highlights were computed after truncation** - with `highlights=True` +
  `max_chars_per_source`, `top_sentences` ran over the already-capped prefix, so a top
  query-relevant sentence past the cap could never surface. Now computed from the full
  pre-cap content (stashed then always stripped, so the payload stays lean either way).
- **`search(category=...)` silently coerced an invalid enum to `general`** (and cached the
  wrong-scope result). Now rejects with `schema_invalid` up front, consistent with
  `read_pdf` / `extract_structured`.
- **`read()` collapsed `blocked_by_antibot` into `fetch_failed`** unlike `scrape`/
  `screenshot`, and all three used a `"antibot" in str(e)` message check that never matched
  the real message (`"...(anti-bot block)"` - hyphenated). All three now derive the code from
  the structured `FetchError.code`; `batch_read` counts an antibot block as `ok=False`.
- **A wedged stealth browser was reused until process restart** - `_bounded_arun` bounded the
  wedge (0.4.0) but kept `_stealth` pointed at the hung crawler. Now recycles it (close+null
  under lock) on timeout so the next call re-inits a fresh one (stealth tier only; the normal
  tier has no lazy re-init).

### Changed (perf)

- **SSRF DNS resolution now runs off the event loop, bounded by a timeout.** `resolve_and_validate`
  called blocking `socket.getaddrinfo` synchronously from 6 async paths (incl. the safe-transport
  send hook, which re-resolves per request AND per redirect hop); on the single worker one slow/hung
  lookup froze ALL concurrent tool calls. New `aresolve_and_validate` runs the same validator via
  `asyncio.to_thread` under `asyncio.timeout(ARGUS_DNS_TIMEOUT`, default 5`)`, re-raising a timeout as
  `SSRFError`. Security logic byte-for-byte identical (validation, IP-pinning, both defence-in-depth
  re-resolves unchanged). New `ARGUS_DNS_TIMEOUT` documented in `deploy/argus.env.example`.

## [0.4.0] - 2026-07-02 - Hardening round 6: multi-agent audit, 30 fixes shipped

A 49-agent workflow (7 module-group analyzers + one adversarial verifier per finding) audited the whole codebase; 41/42 findings survived adversarial review. Everything offline-measurable was shipped, each with regression tests: suite 640 -> 722 passed, ruff clean, coverage total 94% held (touched modules at or above baseline). Root-caused, not symptom-patched.

### Fixed - correctness

- **Cache: missing/corrupt blob no longer raises into tools** - a deleted or truncated blob file (disk cleanup, crash mid-write) made every cached tool throw for the whole TTL and `get_stale` fail forever. Now self-heals: dead row deleted, treated as a cache miss, fresh fetch follows.
- **Cache: `key()` no longer lowercases path/query** - `read("https://host/API")` and `/api` collided onto one cache key and served each other's content for up to an hour. Only scheme+host are case-insensitive per RFC 3986. One-time cold cache for mixed-case keys.
- **Throttle: per-host courtesy delay now holds under concurrency** - N same-host acquirers (batch_read fires 8) all read the stale `last_request` and burst simultaneously after one shared sleep. Slot reservation (write before await) queues them at exactly `min_interval` spacing.
- **Render: challenge pages can no longer masquerade as content** - a success=True "Just a moment..." page (either tier) now raises `blocked_by_antibot` instead of feeding "Verify you are human" into read/scrape/research; fetch core then falls through to its static/Wayback ladder.
- **Render: wedged Chromium cannot starve the pool** - `arun` had no outer bound; a hung CDP pipe held a semaphore permit forever (4 hangs = browser tier dead until restart). `asyncio.timeout(timeout + 15s grace)` converts it to a bounded `render_failed`.
- **Rerank: safety floor backfills instead of replacing** - when relevant results were a minority, the floor branch REPLACED them with the backend's first-N junk; relevant tail hits now always survive (lexical + hybrid paths).
- **Rerank: URL dedup keeps meaningful query params** - `watch?v=AAA` vs `?v=BBB` no longer collapse as duplicates; tracking params (`utm_*`, fbclid, gclid, ...) still dedup; params compare order-insensitively.
- **Relevance guard ignores stopword overlap** - garbage sharing only "to"/"the" with a natural-language query is now flagged `low_relevance` (guard-only change; rerank scoring untouched).
- **Router: modal "may" no longer routes to news** - "what may cause a memory leak" went to the news backend. "may" now only counts as a month when date-anchored.
- **research: per-source isolation restored** - one unexpected extractor error killed the whole deep bundle; now an isolated `{url, error: "extract_failed"}` entry.
- **extract_structured (auto): no more bare `None`** - selector tier raising with no LLM fallback returned `None` to the client; now a structured `extraction_failed` error.
- **PDF: pages-spec errors are honest** - malformed ("abc", "5-") or fully out-of-document specs on a VALID PDF returned `not_pdf`/crash paths; now `schema_invalid`. Reversed ranges auto-swap. Unknown `read_pdf` mode -> `schema_invalid` (docs advertised a nonexistent `figures`).
- **PDF: spec-legal leading junk accepted** - `%PDF-` may sit up to 1024 bytes in (naive proxies/CGI prepend junk; pymupdf parses these fine); the magic gate now searches the first KiB.
- **PDF quality tier honors `pages`** - Docling OCR'd the WHOLE document while reporting `pages_returned` as if sliced; the PDF is now sliced via pymupdf before Docling.
- **map_urls: gzipped sitemaps decompressed** - `.xml.gz` payloads (standard on WordPress/news/large sites) arrived as mojibake and were silently skipped, collapsing discovery to 1-hop links. Magic-byte sniff + decompressed-size cap (zip-bomb guard).
- **Article extractor: only ADJACENT duplicate blocks collapse** - the global dedup deleted legitimate non-consecutive repeats (refrains, repeated legal clauses), contradicting its own docstring.
- **ForexFactory: non-dict feed elements skipped** - one junk element crashed the whole calendar with AttributeError.
- **ForexFactory: unknown impact labels pass through verbatim** - previously folded into "Holiday", hiding a potentially high-impact event class on feed drift; empty still -> "Holiday".
- **ForexFactory: `date_range` validated** - malformed bounds ("2026-6-2", ints, 1-element) raise coded `ff_bad_date_range` instead of silently mis-filtering.
- **watch: failing sources honor `interval_s`** - an errored check never advanced `last_check`, so a broken URL was re-fetched EVERY 60s tick forever (60x load for a 1h watch). Errors now advance the clock, keep the baseline hash, and surface in poll results; a persist OSError on one watch no longer aborts the rest of the tick.
- **crawl: real deadline + real error contract** - `ARGUS_TIMEOUT_CRAWL` (180s) was dead config and `deep_crawl`'s `timeout` param was never read: a tarpit crawl could hold the shared Chromium ~50 minutes. Now: per-page `page_timeout`, whole-crawl `asyncio.timeout` at the tool layer, `depth`/`max_pages` clamped (0-5 / 1-200), crawler exceptions -> structured `fetch_failed` (dead `CrawlError` class removed), and the crawl holds a BrowserPool permit so its page loads count against the RAM guard.
- **cot_report: `date` honored, error codes unmasked** - `date` was silently ignored (wrong-week data served as requested); now filters rows (`requested_date` echoed, non-matching -> honest empty set). `cot_bad_report_type`/`ff_*` codes reach the client instead of being flattened to `fetch_failed`.

### Added

- **Cache eviction** - `Cache.purge(max_age_s=7d)` deletes expired rows + their blobs and sweeps orphaned blob files (a shrink-re-put leaked the old blob forever); runs hourly from the watch loop. Unbounded `~/.argus` growth on the shared VPS (Hermes/SUVA co-tenant) is now bounded.
- **4 uncached tools now cached** - read_pdf (URL only; `pdf` TTL 24h - repeated Docling parses drop from seconds to ms), forexfactory_calendar + cot_report (`trading` 300s), news_sentiment_feed (`news` 900s). These TTLs existed as dead config since P1. Stale-fallback FF bundles are never cached.
- **Degraded results are never cached** - a `low_relevance`/failover search, degraded research bundle, incomplete GitHub scan, or degraded news feed retries next call instead of re-serving junk for the full TTL.
- **`argus_tool_errors_total{code=...}` on /metrics** - errors return as dicts (never raise), so Prometheus previously saw an SSRF block or a dead SearXNG as SUCCESS. Now alertable per error code.
- **smart_search specialist failover** - a dead/rate-limited GitHub/scholar backend (anon 10 req/min) falls back to general search, flagged `degraded: true` + `specialist_failover` instead of returning a dead error.
- **github_search surfaces `incomplete_results`** - GitHub's partial-index-scan flag now maps to the project-wide `degraded`/`degraded_reason` convention.
- **news_sentiment_feed propagates `degraded`** - off-topic or failed-over news is no longer served as clean trading input.
- **COT live drift detectors** - `identity_failures` (composed accounting identity per row) + `bad_dates` (ISO check) on every response; a CFTC column-layout change now lights up live instead of only in the offline golden test.
- **watch poller logging** - the server poll loop logs failures (was a bare `pass`).

### Changed

- **Article metadata extraction ~4x cheaper** - `_metadata` used full `bare_extraction` (re-parses the entire body) for 5 header fields; now `extract_metadata` (measured 1.50 -> 0.38 ms/call on the ad-heavy fixture, identical field values). Hot path of read/scrape/batch_read/crawl/research.
- docs/03-TOOL-SPECS.md refreshed: search/smart_search/github degraded fields, read_pdf modes + caching + `schema_invalid`, crawl `timeout`+clamps, trading contracts (drift detectors, coded errors, cache TTLs), map_urls gzip note.

### Deferred (explicitly)

- `_SEM_FLOOR` recalibration + semantic-rescue/guard alignment: they change rerank scores, so they are gated on the live semantic A/B harness (SearXNG + LLM judge) - not shippable on unit tests alone.
- Proxy pool, LLM tier default-on, multi-worker uvicorn: owner decisions, unchanged by design.

## [0.3.2] - 2026-07-02 - Relevance guard: majority rule

Live dogfooding surfaced a second shape of the same failure the 0.3.1 guard was meant to catch. Root cause confirmed by querying SearXNG directly on the box: on the datacenter IP every quality engine is CAPTCHA/rate-limit **suspended** (brave, duckduckgo, google, mojeek, qwant, startpage) leaving **bing as the sole responder**, and bing under throttle returns generic filler (AOL.com pages for a "kanban orchestration" query; "affect vs effect" grammar pages for a "Nous Research Hermes" query) that SearXNG parses as results. The 0.3.1 guard only fired on **zero** token overlap, so a set where one filler page incidentally shared a single query word slipped through with `degraded: false` (observed in `research` deep mode: Microsoft Copilot/Windows docs returned for a Hermes query).

### Changed

- **Relevance guard is now majority-based.** `search()` flags `degraded: true` + `degraded_reason: "low_relevance"` when **fewer than half** the returned results share a title/snippet token with the query (was: only when *no* result overlapped). A lone incidental token match no longer masks an otherwise off-topic set. On-topic sets (the overwhelming majority overlap) are untouched; the flag remains advisory (nothing dropped, nothing errored).

### Tested

- New regression: majority-off-topic set with one incidental single-token match -> `degraded=true`. Existing zero-overlap and on-topic cases still hold. Full suite green (640 passed).

### Root cause (NOT fixed here - needs an infra decision)

The garbage originates upstream: a single datacenter IP gets CAPTCHA'd by all the good engines, so only a throttled bing survives and serves filler. The detection above makes Argus **honest** about it, but the elimination is the outbound **proxy pool** already scaffolded (commented) in `deploy/searxng/settings.yml` (`outgoing.proxies`) - route SearXNG's engine requests through rotating residential/socks5 proxies so the majors stop throttling. Alternatively, a SearXNG image bump may refresh the bing scraper. Both are owner/cost decisions, left to the operator.

## [0.3.1] - 2026-07-02 - Relevance guard

Follow-up to the concurrency investigation: under parallel `read` + `research` load, SearXNG occasionally returned an entirely off-topic result set (observed: Google Drive pages for a Hermes query), which `search`/`research` surfaced with `degraded: false` - silent garbage. The `search` params path is concurrency-safe (per-call `{**params, ...}`, thread-safe httpx client); the defect was the *absence of a signal* that the returned set was unrelated. Fix is a deterministic, defense-in-depth relevance guard - no behavior change for on-topic queries.

### Added

- **Low-relevance guard in `search()`** - after rerank, if the query has usable tokens (>=2 chars) and **no** returned result shares a title/snippet token with it, the response is flagged `degraded: true` with new field `degraded_reason: "low_relevance"`. Lets the consuming agent (Hermes/Claude Code) retry or discount the batch instead of trusting off-topic hits.
- **`degraded_reason`** field on `search()` responses (`null` when clean; `"backend_failover"` when a fallback backend served the query; `"low_relevance"` per above).

### Fixed

- **`research()` now propagates the search `degraded`/`degraded_reason`** into every bundle (quick/deep/answer), so a low-relevance or failover signal is no longer swallowed by the research layer.

### Tested

- New regression tests: off-topic result set -> `degraded=true` + `low_relevance`; on-topic set stays clean; `research` surfaces the propagated `degraded`. Full suite green (639 passed).

## [0.3.0] - 2026-07-02 - Evidence-based tuning

Multi-agent analysis (14-agent workflow: 7 code deep-dive + 6 external-SOTA research + synthesis) then a 3-agent adversarial review (0 blocking), producing conservative, tested, benchmark-informed tuning. All changes deployed live and reversible. Confirmed already-good (not gaps): hybrid rerank is auto-on and live; the LLM tier is deliberately off by design.

### Added

- **`ARGUS_SEMANTIC_RERANK` env knob** (`auto`|`on`|`off`, default `auto`) - ops kill-switch / in-prod A-B lever over the hybrid semantic rerank (A/B-validated +14.3% nDCG@5, +27.3% on conceptual). `auto` (unset/unknown too) preserves today's behavior: hybrid iff the local embedding stack loads. Documented in `deploy/argus.env.example`.
- **`ARGUS_ENABLE_LLM` documented** in `deploy/argus.env.example` - the previously-undocumented REQUIRED gate for the LLM tier (a key alone never enabled it); clarifies the fail-safe, tools-not-brain design.

### Changed

- **Anti-bot status blocks now escalate** - `fetch_static` raises `FetchError` on HTTP 403/429/503 so `fetch.core` fires the existing stealth-browser + Wayback fallback ladder. Previously a WAF/Cloudflare challenge page (a non-2xx with a body) was returned as if it were content, because the recovery ladder is gated on `except FetchError` and a status block never raised - so it never fired. Makes the static tier consistent with the browser tier's pre-existing block heuristic.
- **Article extraction drops comment threads** - `trafilatura.extract(..., include_comments=False)` on both the markdown and text paths, so Reddit/HN/Disqus comment blocks no longer leak into main content (higher boilerplate rejection, no recall loss on articles).
- **SearXNG penalty box shortened** (`deploy/searxng/settings.yml` `suspended_times`: CAPTCHA 24h->15m, TooManyRequests 1h->5m, AccessDenied 24h->15m) - a single throttled burst no longer benches an engine for hours. Root-cause fix for the DuckDuckGo answer-concentration (was 189/200): keeps the multi-engine fan-out populated so rerank sees a diverse pool. Private loopback instance (`limiter: false`).

### Fixed

- **`research()` throttle bypass** - `research`/`_deep_bundle`/`_read_one` now thread the per-host `HostThrottle`, and the `research` server tool passes `throttle=s.throttle` (every other fetch tool already did). A deep-research call no longer fires parallel same-host fetches with zero courtesy delay and no circuit-breaker - a politeness/reliability defect flagged in `benchmark/reports/RESULTS.md`.

### Tested

- New regression tests: parametrized 403/429/503 static block -> stealth-browser escalation; `research(throttle=X)` forwards the throttle into fetch. Full suite green.

### Follow-ups (deferred, non-blocking)

- 429 escalates without honoring `Retry-After` (consider special-casing vs 403/503); block-escalation widens browser-render load on the single worker (watch read/research p95). Larger deferred items (per the tuning plan): curate forum/PDF benchmark gold, Docling PDF-quality fix, optional cross-encoder rerank / curl_cffi TLS tier.

## [0.2.0] - 2026-06-25 - DEPLOYED LIVE

Live at `https://argus.gifariksuryo.xyz/mcp` on VPS `103.172.172.29` (uvicorn `127.0.0.1:8090 --workers 1`, SearXNG `:8888`, Let's Encrypt TLS, fail2ban). Surfaced and fixed by live end-to-end testing of the deployed MCP.

### Added

- **Safe VPS auto-update** (`deploy/argus-update.sh` + `.service` + `.timer`) - pull-only (no inbound port) poll of `main` every 5 min, **fast-forward only**, reinstall deps only on manifest change, restart, `/health`-gate, and **auto-rollback** to the prior commit on failure. Hardened against mode-drift; **skips restart on docs-only changes**. Runbook in [`deploy/README.md`](deploy/README.md).
- **Cache WAL** - SQLite write-ahead logging for concurrent-reader durability under load.

### Changed

- **research (deep mode)** - `MIN_CONTENT_WORDS=30` low-content floor moves near-empty stub pages (e.g. a bare video page) to `failed` as `low_content`; **source backfill** keeps pulling from the overfetched candidate pool until `max_sources` GOOD sources or the pool is exhausted (failures no longer shrink the bundle; the happy path does no extra fetches). Added `max_chars_per_source` to bound per-source payload.
- **scholar_search** - retry Semantic Scholar on HTTP 429 (2x bounded backoff) so the richer S2 backend is used; **citation-rerank** by query/title overlap then citation count, so the canonical highly-cited paper beats derivative "X is All You Need" titles.
- **search** - Docker/generic-token tuning plus a gentle relative-relevance gate (`_REL_FLOOR=0.25`) that trims clearly-weak backfill (off-topic single-generic-token matches) without hurting recall or the `_MIN_KEEP=3` floor; consistent across the lexical and hybrid paths.

### Fixed

- **Stealth race** - resolved a concurrency race in the stealth-browser escalation path.

### Benchmarked

- **4-way harness** (`benchmark/run_4way.py`) + **n=25** head-to-head results recorded; `research()` runs **3-6s in-process** (Argus is not the bottleneck; observed CLI latency is agent + transport, not the server). See [`benchmark/reports/RESULTS.md`](benchmark/reports/RESULTS.md).

### Security / QA

- Security + cleanup + coverage round: **600 offline tests** green (plus browser + slow); SSRF 100%; ruff clean.

### Docs

- Status refresh across `CHANGELOG`, `docs/00-DESIGN.md`, and `docs/02-ROADMAP.md` to reflect the live deployment and 20-tool surface.

---

## [0.1.0] - 2026-06-24 - feature-complete build (20 tools)

The full local build: research to a 20-tool, security-audited, benchmarked FastMCP server, productionized with deploy artifacts.

### Added - tools (6 -> 20)

- `smart_search` - deterministic query-to-domain auto-router (no LLM).
- `scholar_search` - structured academic search (Semantic Scholar -> CrossRef).
- `github_search` - structured GitHub repos/code/issues.
- `map_urls` - sitemap/robots/link URL discovery.
- `find_similar` - local-embedding semantic similarity (Exa-style).
- `research(deep/quick/answer)` - one-shot research bundles + `highlights`.
- `watch` / `list_watches` / `unwatch` - poll -> diff -> webhook monitoring.
- `read(extract_media)` - links + images extraction.
- `extract_structured` LLM/auto tier (optional).

### Added - capability

- **Local semantic search** (`semantic.py`, fastembed bge-small, ONNX, no torch) -> hybrid rerank (**+27% nDCG on conceptual queries**, A/B `benchmark/semantic_ab.py`) + `find_similar` + highlights.
- **Egress fallback** - stealth browser -> Wayback archive on connect-fail / anti-bot.
- **Search resilience** - multi-engine redundancy, auto-backoff on throttle, rerank v2, domain filters + safesearch.

### Hardening & ops

- **Streaming body-size cap** - `fetch/static.py` aborts a chunked / no-Content-Length body once it exceeds 32 MB (a true OOM guard, not just the header check).
- **Per-host throttle + circuit breaker** - `fetch/throttle.py`: courtesy delay between same-host requests + closed -> open -> half-open breaker; wired into `fetch()` (default-off in tests).
- **Caching everywhere** - `research` / `scholar_search` / `github_search` / `map_urls` now cache (store-good-only, per-source TTL) alongside `read` / `search`.
- **`/metrics`** - per-tool request counters via a signature-safe FastMCP middleware.
- **Auth** - `JWTVerifier` (prod) takes precedence over `StaticTokenVerifier` (dev) via env.
- **LLM is opt-in and off by default** (`ARGUS_ENABLE_LLM`) - Argus is tools-not-brain; the consuming agent synthesizes.

### Benchmarked

- 200-scenario Argus run + 3-way head-to-head vs Claude Code and Codex native (n=50): discovery parity, Argus wins on full-content depth (~7k words/query). See [`benchmark/reports/RESULTS.md`](benchmark/reports/RESULTS.md).
- Competitor feature-gap analysis -> adopted the self-hostable gaps. See [`docs/05-COMPETITIVE-GAP.md`](docs/05-COMPETITIVE-GAP.md).

### Security / QA

- Multi-agent QA/QC end-to-end: 526 offline + browser + slow green; SSRF 100%; ruff clean.
- Security audit Round 1 + 2 ([`deploy/SECURITY-AUDIT.md`](deploy/SECURITY-AUDIT.md)): no Critical / High; fixes applied (kwarg bug, prompt-injection hardening, public-suffix scope, never-raise catch-alls, embedder lock).
- Repo tidied; `.gitignore` consolidated.

### Docs

- Rich `README.md` (banner + architecture SVG, shields badges, typing animation), `SOUL.md`, `AGENTS.md`, this `CHANGELOG.md`.

---

## Build phases (P0 -> P3)

<details>
<summary>Phase-by-phase build record</summary>

### P3 - productionize + deploy

- Streamable-HTTP transport (`uvicorn argus.server:app`), `/health` + `/metrics`, bearer/JWT auth.
- `read_pdf` local-path LFI locked down (`ARGUS_ALLOW_LOCAL_PDF`, default-off on remote).
- Deploy artifacts: `argus.service`, `argus.nginx.conf`, `provision.sh`, `fail2ban-argus.conf`, [`deploy/README.md`](deploy/README.md).
- Local load test passed (no OOM/leak). Deployed live 2026-06-25 (see `[0.2.0]`).

### P2 - feature parity + trading moat

- `crawl`, `screenshot`, `extract_structured` LLM tier, Docling PDF tier, Patchright stealth.
- Trading extractors `forexfactory_calendar` / `cot_report` / `news_sentiment_feed` - **100% golden-file field accuracy** (>=99% gate).
- Benchmark quality gate moved to a formatting-invariant `quality_f1` (raw-text ROUGE-L was confounded) - Argus ties the best free baseline.

### P1 - MVP, validated locally

- 6 tools: `read / search / read_pdf / scrape / batch_read / extract_structured`.
- **SSRF guard at 100% coverage** (hard gate), content-addressed cache, tiered fetch (httpx -> Crawl4AI), SearXNG, FastMCP stdio.

### P0 - research and design

- 12-tool incumbent survey, OSS stack decision, design + roadmap + tool specs + benchmark testset.

</details>

---

<div align="center"><sub>SURIOTA / self-hosted / unlimited / owned</sub></div>
