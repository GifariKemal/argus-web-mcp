# Argus X module, Phase A (Layer 0) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Read and search X posts without an X account: `read` on x.com/twitter.com URLs returns a clean post or profile via FxTwitter, and a new `x_search` tool finds posts through SearXNG `site:x.com` and enriches them via FxTwitter.

**Architecture:** New package `src/argus/x/` with two files: `fxtwitter.py` (URL targeting, FxTwitter client, post/user models, HTML rendering) and `index.py` (search -> filter status URLs -> enrich). The fetch ladder routes matching x.com URLs to FxTwitter *before* the static hop (unlike the StackExchange/npm adapters, which only run after a static failure, because x.com answers 200 with a noisy page). `server.py` registers `x_search` (tool 21).

**Tech Stack:** Python 3.12, httpx via `fetch_static` (SSRF-guarded), FastMCP, pytest + respx, existing SearXNG wrapper `searxng_search`.

**Spec:** `docs/plans/2026-10-30-argus-x-module-design.md` (Phase A rows only; Layer 1 is a separate plan).

## Global Constraints

- No new dependencies. No X account, no cookies, no `x_*` error codes in this phase (they belong to Layer 1).
- Every outbound request goes through `fetch_static(url, client=..., timeout=...)` so the SSRF guard and size cap apply.
- FxTwitter base URL: env `ARGUS_X_FXTWITTER_URL`, default `https://api.fxtwitter.com`, read in `config.py`.
- Post model fields exactly: `{id, url, created_at, text, lang, author: {handle, name, verified, followers}, metrics: {replies, reposts, likes, quotes, views, bookmarks}, media: [{type, url}], quoted, in_reply_to, source}`.
- `x_search` cache TTL: 300 s for `mode="latest"`, 1800 s for `mode="top"`.
- `x_search` limits: `limit` clamped to 1-50; enrichment concurrency 4.
- Errors only from the existing `ERROR_CODES` (`schema_invalid`, `no_results`, `search_backend_down`).
- All fetched text is untrusted: HTML-escape every field when rendering.
- Docs and code ASCII only (pre-commit `scripts/sanitize_md.py`); ruff clean; SSRF coverage gate untouched.

## Review Focus

- Lookalike hosts (`x.com.evil.example`, `notx.com`) and odd paths (`/i/web/status/<id>`, `/<u>/status/<id>/photo/1`, `?s=20`) must route correctly or not at all: Task 1 `test_x_target`.
- Reserved first segments (`/home`, `/search`, `/explore`, `/i`, `/settings`, `/hashtag/x`) must never be treated as profiles: Task 1 `test_x_target`.
- FxTwitter answers HTTP 200 with an HTML page for deleted/unknown posts: client returns None, `read` falls through to the normal ladder, `x_search` drops that id: Task 1 `test_fx_post_non_json_is_none`, Task 2 `test_falls_back_to_static_when_fxtwitter_fails`, Task 3 `test_x_search_drops_unenrichable_ids`.
- Post text carrying markup (`<script>`, `</article>`) must come out escaped, never as live HTML: Task 2 `test_html_escapes_post_text`.
- Bad dates (`2026-02-30`, `since` after `until`, fullwidth digits) must be `schema_invalid`, not a crash or an empty search: Task 3 `test_x_search_rejects_bad_dates`.

---

### Task 1: FxTwitter client and models

**Files:**
- Create: `src/argus/x/__init__.py` (empty docstring module)
- Create: `src/argus/x/fxtwitter.py`
- Modify: `src/argus/config.py` (add `X_FXTWITTER_URL`)
- Test: `tests/test_x_fxtwitter.py`
- Fixtures (already captured 2026-10-30): `tests/fixtures/x/fx_status_media.json`, `fx_status_plain.json`, `fx_user.json`, `fx_not_found.html`

**Interfaces:**
- Produces:
  - `config.X_FXTWITTER_URL: str` (env `ARGUS_X_FXTWITTER_URL`, trailing `/` stripped)
  - `x_target(url: str) -> tuple[Literal["status", "user"], str] | None` -> `("status", "<id>")` or `("user", "<handle>")`
  - `async fx_post(post_id: str, *, client, timeout: float = 30) -> dict | None` (post model, `source="fxtwitter"`; GET `{base}/status/{id}`)
  - `async fx_user(handle: str, *, client, timeout: float = 30) -> dict | None` -> `{handle, name, verified, followers, following, posts, description, url}` (GET `{base}/{handle}`)
  - `post_from_fx(tweet: dict) -> dict` (pure mapping, used by Layer 1 tests later)

- [ ] **Step 1: Write the failing tests**

```python
@pytest.mark.parametrize(("url", "expected"), [
    ("https://x.com/jack/status/20", ("status", "20")),
    ("https://twitter.com/jack/status/20?s=20", ("status", "20")),
    ("https://mobile.twitter.com/jack/statuses/20", ("status", "20")),
    ("https://www.x.com/cnxsoft/status/1957667536302141541/photo/1", ("status", "1957667536302141541")),
    ("https://x.com/i/web/status/20", ("status", "20")),
    ("https://x.com/XDevelopers", ("user", "XDevelopers")),
    ("https://x.com/XDevelopers/", ("user", "XDevelopers")),
    ("https://x.com/home", None), ("https://x.com/search?q=a", None), ("https://x.com/explore", None),
    ("https://x.com/i/flow/signup", None), ("https://x.com/settings", None), ("https://x.com/hashtag/esp32", None),
    ("https://x.com/jack/likes", None),
    ("https://x.com.evil.example/jack/status/20", None), ("https://notx.com/jack/status/20", None),
    ("http://example.com/jack/status/20", None), ("https://x.com/a_name_way_too_long_x/status/1", None),
])
def test_x_target(url, expected): assert x_target(url) == expected

def test_post_from_fx_media_fixture():
    p = post_from_fx(load("fx_status_media.json")["tweet"])
    assert p["id"] == "1957667536302141541"
    assert p["url"] == "https://x.com/cnxsoft/status/1957667536302141541"
    assert p["created_at"] == "2025-08-19T04:54:47Z"
    assert p["text"].startswith("ESP32 Modbus industrial IoT gateway")
    assert "https://www.cnx-software.com/2025/08/19/" in p["text"]     # expanded, not t.co
    assert p["author"]["handle"] == "cnxsoft" and p["author"]["verified"] is True
    assert p["metrics"] == {"replies": 1, "reposts": 33, "likes": 295, "quotes": 0,
                            "views": 12319, "bookmarks": 117}
    assert p["media"][0] == {"type": "photo", "url": "https://pbs.twimg.com/media/GysIKg0bMAAgnP7.jpg?name=orig"}
    assert p["quoted"] is None and p["in_reply_to"] is None and p["source"] == "fxtwitter"

@respx.mock
async def test_fx_post_and_user(): ...      # mock {base}/status/20 -> fx_status_plain.json, {base}/XDevelopers -> fx_user.json
                                            # assert text "just setting up my twttr"; user handle "XDevelopers", followers 697736, posts 4381

@respx.mock
async def test_fx_post_non_json_is_none():  # 200 text/html fx_not_found.html -> None; 500 -> None; {"code":404} JSON -> None
```

(`load(name)` reads `tests/fixtures/x/<name>` with `encoding="utf-8"`; module uses the `public_dns` autouse fixture like `tests/test_adapters.py`.)

- [ ] **Step 2: Run to verify they fail**

Run: `./.venv/Scripts/python.exe -m pytest tests/test_x_fxtwitter.py -q`
Expected: FAIL, `ModuleNotFoundError: No module named 'argus.x'`

- [ ] **Step 3: Implement `src/argus/x/fxtwitter.py` and `config.X_FXTWITTER_URL`**

- Hosts accepted: exactly `x.com`, `twitter.com`, `mobile.twitter.com`, `mobile.x.com`, each optionally with `www.`; scheme http/https.
- Handle regex `[A-Za-z0-9_]{1,15}`; status path `/<handle>/status(es)?/<digits 1-20>` with any suffix, or `/i/web/status/<digits>`; profile path `/<handle>` with optional trailing slash only.
- Reserved first segments (never a profile): `home, explore, search, i, settings, notifications, messages, compose, login, logout, signup, tos, privacy, hashtag, intent, share, account, jobs, premium`.
- `created_at` from `created_timestamp` as UTC `%Y-%m-%dT%H:%M:%SZ`; `reposts` = FxTwitter `retweets`; `media` from `media.all` (`type`, `url` only); `quoted` = `post_from_fx(tweet["quote"])` when present; `in_reply_to` = `replying_to_status`; `verified` = `author.verification.verified`, default False; `posts` = user `tweets`.
- Client accepts only parsed JSON with `code == 200` and a `tweet`/`user` dict; any exception or other shape -> None (never raises, same contract as `fetch_npm`).

- [ ] **Step 4: Run tests**

Run: `./.venv/Scripts/python.exe -m pytest tests/test_x_fxtwitter.py -q`
Expected: all PASS

- [ ] **Step 5: Commit**

```bash
git add src/argus/x tests/test_x_fxtwitter.py tests/fixtures/x src/argus/config.py
git commit -m "feat(x): FxTwitter client, URL targeting and post model"
```

### Task 2: `read` routes x.com URLs through FxTwitter

**Files:**
- Modify: `src/argus/x/fxtwitter.py` (add `render_html`, `fetch_x`)
- Modify: `src/argus/fetch/core.py` (`_do_fetch`, non-forced path, before the static hop)
- Modify: `src/argus/server.py` (`read`: copy `res["structured"]` into `metadata["structured"]`)
- Test: `tests/test_x_fxtwitter.py`, `tests/test_server_integration.py`

**Interfaces:**
- Consumes: `x_target`, `fx_post`, `fx_user` (Task 1)
- Produces:
  - `render_html(post: dict | None = None, user: dict | None = None) -> str` (`<html><head><title>..</title></head><body><article>..</article></body></html>`, every field `html.escape`d)
  - `async fetch_x(url: str, *, client, timeout: float = 30) -> dict | None` -> `{final_url: url, status: 200, html, render_path: "api", structured: post_or_user}`; None when not an X URL or FxTwitter failed

- [ ] **Step 1: Write the failing tests**

```python
def test_html_escapes_post_text():
    p = post_from_fx(load("fx_status_plain.json")["tweet"]) | {"text": "<script>x()</script></article>"}
    h = render_html(post=p)
    assert "<script>" not in h and "&lt;script&gt;" in h and h.count("</article>") == 1

@respx.mock
async def test_read_x_status_uses_fxtwitter(app_state):     # in tests/test_server_integration.py
    fx = respx.get(f"{FX}/status/20").mock(return_value=httpx.Response(200, json=load("fx_status_plain.json")))
    page = respx.get("https://x.com/jack/status/20")
    r = await server.read("https://x.com/jack/status/20")
    assert fx.called and not page.called
    assert r["render_path"] == "api" and "just setting up my twttr" in r["content"]
    assert r["metadata"]["structured"]["id"] == "20"

@respx.mock
async def test_falls_back_to_static_when_fxtwitter_fails(): # FxTwitter 200 HTML -> fetch() GETs x.com page, render_path "static"

async def test_forced_render_skips_x_adapter():             # fetch(url, render=True, browser=fake) never calls fetch_x
```

- [ ] **Step 2: Run to verify they fail**

Run: `./.venv/Scripts/python.exe -m pytest tests/test_x_fxtwitter.py tests/test_server_integration.py -q -k "x_ or escapes or fxtwitter"`
Expected: FAIL (`render_html` / `fetch_x` undefined; read returns `render_path` static)

- [ ] **Step 3: Implement**

- `fetch_x`: `x_target(url)`; status -> `fx_post`, user -> `fx_user`; build `render_html`; `record_stage("fetch.x_api_ok")` / `("fetch.x_api_fail")` in core.
- `core._do_fetch`: after the `force_browser` branch and before `fetch_static`, `if x_target(url): api = await fetch_x(...); if api: return api`. Run `_guard(url)` first is NOT needed (fetch_x only contacts the FxTwitter base through `fetch_static`, which guards it).
- `server.read`: after extraction, `if res.get("structured"): meta["structured"] = res["structured"]`.

- [ ] **Step 4: Run the full offline suite**

Run: `./.venv/Scripts/python.exe -m pytest -q -m 'not browser and not slow and not network'`
Expected: all PASS (existing 1108 + new)

- [ ] **Step 5: Commit**

```bash
git add src/argus/x/fxtwitter.py src/argus/fetch/core.py src/argus/server.py tests/test_x_fxtwitter.py tests/test_server_integration.py
git commit -m "feat(x): read x.com posts and profiles through FxTwitter"
```

### Task 3: `x_search` tool (index tier)

**Files:**
- Create: `src/argus/x/index.py`
- Modify: `src/argus/server.py` (tool `x_search`, add to `TOOLS` after `scholar_search`)
- Modify: `tests/test_server_surface.py` (`len(tools) == 21`)
- Test: `tests/test_x_index.py`

**Interfaces:**
- Consumes: `x_target`, `fx_post` (Task 1)
- Produces:
  - `async search_posts(query: str, *, mode: Literal["top", "latest"], limit: int, since: date | None, until: date | None, search_fn, client) -> dict` -> `{results: [post], source: "index", degraded: bool, degraded_reason: str | None}`; raises whatever `search_fn` raises (`SearchError`)
  - tool `x_search(query: str, mode: Literal["top", "latest"] = "top", limit: int = 10, since: str | None = None, until: str | None = None) -> dict` -> `{query, mode, source, results, count, degraded, degraded_reason, from_cache}`

- [ ] **Step 1: Write the failing tests**

```python
async def test_x_search_filters_dedupes_and_enriches(monkeypatch):
    # search_fn returns: 2 status URLs for the same id (x.com + twitter.com), 1 profile URL, 1 wikipedia URL, 1 more status
    # fx_post patched to return a post per id
    res = await search_posts("esp32 modbus", mode="top", limit=10, since=None, until=None, search_fn=fake, client=None)
    assert [p["id"] for p in res["results"]] == ["111", "222"]
    assert res["degraded"] is False and res["source"] == "index"
    assert fake.kwargs["include_domains"] == ["x.com", "twitter.com"]

async def test_x_search_latest_sorts_newest_first_and_flags_index_only(): ...
    # assert ids ordered by created_at desc; degraded True; degraded_reason == "index_only"

async def test_x_search_drops_unenrichable_ids(): ...     # fx_post None for one id -> that id absent, others kept
async def test_x_search_caps_at_limit_and_concurrency(): ... # 30 ids, limit 5 -> 5 fx_post calls; never > 4 in flight
async def test_x_search_date_window(): ...                # since/until drop posts outside [since 00:00Z, until 23:59:59Z]

@pytest.mark.parametrize("since,until", [("2026-02-30", None), ("2026-10-30", "2026-10-01"),
                                          ("\uff12\uff10\uff12\uff16-10-01", None), ("30-10-2026", None)])
async def test_x_search_rejects_bad_dates(app_state, since, until):
    r = await server.x_search("esp32", since=since, until=until)
    assert r["code"] == "schema_invalid"

async def test_x_search_tool_caches_per_mode(app_state, monkeypatch): ...
    # second identical call from_cache True; mode="latest" is a separate cache row
async def test_x_search_maps_backend_errors(app_state, monkeypatch): ...
    # SearchError("no_results") -> code no_results; other -> search_backend_down; zero enriched -> no_results
```

- [ ] **Step 2: Run to verify they fail**

Run: `./.venv/Scripts/python.exe -m pytest tests/test_x_index.py tests/test_server_surface.py -q`
Expected: FAIL (`argus.x.index` missing; tool count 20)

- [ ] **Step 3: Implement**

- `search_posts`: call `search_fn(query, count=min(limit * 3, 60), include_domains=["x.com", "twitter.com"])`; keep results whose `x_target` is `("status", id)`, dedupe by id preserving rank, cut to `limit`; enrich with `asyncio.Semaphore(4)`; drop None; date filter on `created_at`; `latest` -> sort by `created_at` desc and set `degraded`/`degraded_reason="index_only"`. Stages: `record_stage("x.index")` once per call, `record_stage("x.fxtwitter")` per successful enrichment (spec Observability).
- Tool: validate dates with `re.fullmatch(r"[0-9]{4}-[0-9]{2}-[0-9]{2}", s)` then `date.fromisoformat` (ASCII-only, same rule as `cot_bad_date`), `since <= until`; clamp `limit` 1-50; cache key `"x_search:" + query` with `{mode, limit, since, until}`, TTL 300 (latest) / 1800 (top); never cache degraded-by-error results; pass `search_fn=searxng_search` (module attribute, so tests monkeypatch `server.searxng_search`).
- Docstring (tool description seen by agents): "Search X/Twitter posts by keyword (X search syntax). No account: finds indexed posts via web search and returns full text, author, date and metrics; not real-time (mode='latest' is flagged degraded). Read one post or profile with `read` on its x.com URL."

- [ ] **Step 4: Run tests and lint**

Run: `./.venv/Scripts/python.exe -m pytest -q -m 'not browser and not slow and not network'` then `./.venv/Scripts/ruff.exe check src tests benchmark`
Expected: all PASS; ruff "All checks passed!"

- [ ] **Step 5: Commit**

```bash
git add src/argus/x/index.py src/argus/server.py tests/test_x_index.py tests/test_server_surface.py
git commit -m "feat(x): x_search tool over the web index with FxTwitter enrichment"
```

### Task 4: Docs, version, smoke, release check

**Files:**
- Modify: `docs/03-TOOL-SPECS.md` (21 tools; `x_search` section; `read` notes x.com -> `render_path: "api"`)
- Modify: `CLAUDE.md`, `AGENTS.md`, `README.md`, `docs/STATUS.md`, `docs/02-ROADMAP.md`, `CHANGELOG.md` (every "20 tools/20 live" -> 21; CHANGELOG 0.4.35 entry with attribution line for FxTwitter)
- Modify: `pyproject.toml` + `src/argus/__init__.py` (version `0.4.35`)
- Modify: `src/argus/reachability.py` (`SITES["api.fxtwitter.com"] = "https://api.fxtwitter.com/status/20"`), `tests/test_reachability.py` if it pins the site count
- Modify (outside repo, owner server folder): `C:\Users\Administrator\Music\SuriotaPrimaryServer\argus\mcp_smoke.py` `CALLS["x_search"] = {"query": "esp32 modbus", "limit": 3}`

- [ ] **Step 1: Find every stale count**

Run: `grep -rn "20 live\|20 tools\|(20 \|== 20" --include=*.md --include=*.py . | grep -v .venv`
Expected: a list; update each to 21 (tests already at 21 from Task 3).

- [ ] **Step 2: Apply the doc/version edits above**

- [ ] **Step 3: Full verification**

Run: offline suite, `./.venv/Scripts/python.exe -m pytest tests/test_ssrf.py --cov=argus.security.ssrf --cov-branch --cov-fail-under=100 -q`, ruff, `./.venv/Scripts/python.exe scripts/sanitize_md.py` on changed docs.
Expected: all green; SSRF 100%.

- [ ] **Step 4: Live check against a local stack (no production change)**

Run: `docker compose -f docker-compose.yml -f docker-compose.local.yml up -d`, then call `x_search("esp32 modbus", limit=3)` and `read("https://x.com/jack/status/20")` on `http://127.0.0.1:8090/mcp`.
Expected: `x_search` count >= 1 with full text; `read` render_path "api".

- [ ] **Step 5: Commit (local only; push and deploy need the owner's OK)**

```bash
git add -A docs CLAUDE.md AGENTS.md README.md CHANGELOG.md pyproject.toml src/argus/__init__.py src/argus/reachability.py tests
git commit -m "release: 0.4.35 - x_search and x.com read via FxTwitter (X module Phase A)"
```
