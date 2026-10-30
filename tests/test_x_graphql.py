"""X module, Layer 1: SearchTimeline with one cookie account (offline).

``fixtures/x/gql_search.json`` is twscrape's ``raw_search.json`` (MIT, (c) 2023 vladkens),
trimmed. ``fixtures/x/xclid_vector.json`` holds a synthetic page and the anim key and
transaction id the original twscrape code computed for it (seeded salt, fixed clock).
"""

import json
import random
from pathlib import Path
from urllib.parse import unquote

import pytest

from argus import config, server
from argus.fetch.static import FetchError
from argus.x import graphql, xclid
from argus.x.graphql import Limiter, XError

FIX = Path(__file__).parent / "fixtures" / "x"
SECRET_AUTH = "AUTHTOKEN0123456789abcdef"
SECRET_CT0 = "CT0VALUE0123456789abcdef"


def _search_data() -> dict:
    return json.loads((FIX / "gql_search.json").read_text(encoding="utf-8"))["data"]


# --- xclid (vendored) -------------------------------------------------------------

def test_xclid_matches_original_twscrape_vector():
    v = json.loads((FIX / "xclid_vector.json").read_text(encoding="utf-8"))
    gen = xclid.XClIdGen.from_page(v["page"], v["ind"])
    assert gen.anim_key == v["key"]
    tx = gen.calc("GET", "/i/api/graphql/abc/SearchTimeline", now=1761800000.0,
                  salt=random.Random(0).randint(0, 255))
    assert tx == v["tx"]


def test_xclid_rejects_logged_out_and_odd_pages():
    with pytest.raises(xclid.XClIdError, match="logged-out"):
        xclid.get_scripts_list(
            '<script src="https://abs.twimg.com/x-web/a/entry-client-logged-out-1.js">')
    with pytest.raises(xclid.XClIdError):
        xclid.XClIdGen.from_page("<html></html>", "(a[1], 16)")


# --- manifest -----------------------------------------------------------------------

BUNDLE = ('e.exports={queryId:"AbCdEfGhIjKlMnOpQrStUv",operationName:"SearchTimeline",'
          'operationType:"query",metadata:{featureSwitches:["rweb_video_screen_enabled",'
          '"brand_new_flag"],fieldToggles:[]}}')


ALL_OPS = BUNDLE + "".join(
    f'e.exports={{queryId:"{q}",operationName:"{op}",operationType:"query",'
    'metadata:{featureSwitches:["rweb_video_screen_enabled"],fieldToggles:[]}}'
    for op, q in (("TweetDetail", "TwEeTdEtAiLqUeRyId1234"),
                  ("UserByScreenName", "UsErByScReEnNaMeId1234"),
                  ("UserTweets", "UsErTwEeTsQuErYiD12345")))


def test_manifest_reads_query_id_and_features_from_bundle():
    assert graphql.query_id_for(BUNDLE, "SearchTimeline") == "AbCdEfGhIjKlMnOpQrStUv"
    assert graphql.features_for(BUNDLE, "SearchTimeline") == {
        "rweb_video_screen_enabled": True, "brand_new_flag": False}
    assert graphql.query_id_for(BUNDLE, "TweetDetail") is None
    assert graphql.query_id_for('queryId:"x<y",operationName:"SearchTimeline"',
                                "SearchTimeline") is None


# --- limiter --------------------------------------------------------------------------

class Clock:
    def __init__(self):
        self.t = 1000.0

    def __call__(self):
        return self.t


async def _no_sleep(_):
    return None


async def test_limiter_window_daily_cap_and_cooldown(monkeypatch):
    monkeypatch.setattr(graphql.asyncio, "sleep", _no_sleep)
    clock = Clock()
    lim = Limiter(per_window=2, window_s=900, per_day=3, clock=clock)
    await lim.acquire()
    await lim.acquire()
    with pytest.raises(XError) as e:
        await lim.acquire()
    assert e.value.code == "x_rate_limited" and 0 < e.value.retry_after <= 901
    clock.t += 901
    await lim.acquire()  # new window, third call of the day
    clock.t += 901
    with pytest.raises(XError):  # daily cap
        await lim.acquire()
    clock.t += 86400
    lim.cool(600)
    with pytest.raises(XError) as e:
        await lim.acquire()
    assert e.value.retry_after == 601


# --- parsing ---------------------------------------------------------------------------

def test_parse_search_maps_posts_and_cursor():
    posts, cursor = graphql.parse_search(_search_data())
    assert [p["author"]["handle"] for p in posts] == ["Tesla", "techiedani", "aakhriastha"]
    p = posts[0]
    assert p["url"] == "https://x.com/Tesla/status/2102107015422304419"
    assert p["created_at"] == "2026-09-21T18:45:42Z" and p["source"] == "graphql"
    assert "https://t.co/" not in p["text"] and "Tesla.com/drive" in p["text"]
    assert p["metrics"]["views"] == 1081124 and p["metrics"]["likes"] == 5474
    assert p["quoted"]["author"]["handle"] == "JSeyff" and p["quoted"]["quoted"] is None
    assert posts[1]["media"] == [{"type": "photo",
                                  "url": "https://pbs.twimg.com/media/HSwpsp6awAAakYY.jpg"}]
    assert cursor and cursor.startswith("DAAD")


def test_parse_search_skips_odd_entries():
    data = _search_data()
    entries = data["search_by_raw_query"]["search_timeline"]["timeline"]["instructions"][0][
        "entries"]
    entries.insert(0, {"content": {"itemContent": {"tweet_results": {"result": {
        "__typename": "TweetTombstone"}}}}})
    entries.insert(0, {"content": {"itemContent": {"tweet_results": {"result": {
        "__typename": "Tweet", "legacy": {"full_text": 7}, "views": {"count": []}}}}}})
    posts, _ = graphql.parse_search(data)
    assert [p["author"]["handle"] for p in posts][-3:] == ["Tesla", "techiedani",
                                                           "aakhriastha"]
    assert graphql.parse_search({}) == ([], None)


def test_raw_query_until_is_inclusive():
    from datetime import date
    assert graphql.raw_query(" esp32 ", date(2026, 10, 1), date(2026, 10, 30)) == (
        "esp32 since:2026-10-01 until:2026-10-31")


# --- calls, errors, secrets ---------------------------------------------------------

def _ops(qid: str = "Q" * 22) -> dict:
    return {op: (qid, {"f": True}) for op in graphql.OPS}


@pytest.fixture
def layer1(monkeypatch, tmp_path):
    """Layer 1 on with fake cookies, a ready manifest, no pacing sleeps, and a session
    file in tmp (never the real ~/.argus)."""
    monkeypatch.setattr(graphql, "SESSION_PATH", tmp_path / "x_session.json")
    monkeypatch.setattr(graphql, "_ct0_live", None)
    monkeypatch.setattr(graphql, "_session_loaded", False)
    monkeypatch.setattr(config, "X_AUTH_TOKEN", SECRET_AUTH)
    monkeypatch.setattr(config, "X_CT0", SECRET_CT0)
    monkeypatch.setattr(graphql.asyncio, "sleep", _no_sleep)
    monkeypatch.setattr(graphql, "_limiter", None)
    monkeypatch.setattr(graphql, "_state", "unknown")
    boots = []

    async def fake_boot(client):
        boots.append(1)
        # 700 s old: past the forced-refresh floor, well inside the 6 h TTL
        return {"gen": xclid.XClIdGen([1] * 48, "k"), "ops": _ops(),
                "at": graphql.time.monotonic() - 700}

    monkeypatch.setattr(graphql, "_boot", None)
    monkeypatch.setattr(graphql, "_boot_fail", None)
    monkeypatch.setattr(graphql, "_bootstrap", fake_boot)
    return boots


def _fake_fetch(monkeypatch, answers):
    """fetch_static stub: pops one answer per call (dict body or Exception)."""
    calls = []

    async def fake(url, *, client, timeout=30, max_redirects=5, headers=None, api=False):
        calls.append({"url": url, "max_redirects": max_redirects, "headers": headers})
        a = answers.pop(0)
        if isinstance(a, Exception):
            raise a
        status, body, *cookies = a
        return {"final_url": url, "status": status, "html": json.dumps(body),
                "render_path": "static", "set_cookie": cookies[0] if cookies else []}

    monkeypatch.setattr(graphql, "fetch_static", fake)
    return calls


async def test_search_sends_cookies_without_redirects_and_pages(layer1, monkeypatch):
    calls = _fake_fetch(monkeypatch, [(200, {"data": _search_data()})] * 3)
    res = await graphql.search_posts("tesla from:Tesla", mode="latest", limit=5, since=None,
                                     until=None, client=None)
    assert res["source"] == "graphql" and not res["degraded"]
    assert len(res["results"]) == 3  # page 2 repeats page 1: no fresh posts, stop
    assert len(calls) == 2
    first = calls[0]
    assert first["max_redirects"] == 0
    assert first["headers"]["x-csrf-token"] == SECRET_CT0
    assert f"auth_token={SECRET_AUTH}" in first["headers"]["cookie"]
    assert first["url"].startswith("https://x.com/i/api/graphql/" + "Q" * 22 + "/SearchTimeline?")
    assert "%22product%22%3A%22Latest%22" in first["url"]
    assert "cursor" in calls[1]["url"]
    assert graphql.status() == {"x_account": "ok", "x_manifest_age_s": 700}


@pytest.mark.parametrize(("answer", "code", "state"), [
    ((200, {"errors": [{"code": 88, "message": "Rate limit exceeded"}]}),
     "x_rate_limited", "rate_limited"),
    ((200, {"errors": [{"code": 326, "message": "denied"}]}), "x_account_locked", "locked"),
    ((200, {"errors": [{"code": 353, "message": "csrf"}]}), "x_account_expired", "expired"),
    (FetchError("blocked_by_antibot", "status 403", status=403), "x_account_rejected",
     "rejected"),
    (FetchError("blocked_by_antibot", "status 429", status=429), "x_rate_limited",
     "rate_limited"),
    ((401, {}), "x_account_expired", "expired"),
    ((200, {"nodata": 1}), "x_schema_changed", "schema_changed"),
])
async def test_search_maps_x_errors_and_never_leaks_cookies(layer1, monkeypatch, answer, code,
                                                            state):
    _fake_fetch(monkeypatch, [answer])
    with pytest.raises(XError) as e:
        await graphql.search_posts("esp32", mode="top", limit=5, since=None, until=None,
                                   client=None)
    assert e.value.code == code
    assert graphql.status()["x_account"] == state
    text = f"{e.value!r} {e.value} {graphql.status()}"
    assert SECRET_AUTH not in text and SECRET_CT0 not in text


async def test_rate_limit_answer_starts_cooldown(layer1, monkeypatch):
    _fake_fetch(monkeypatch, [(200, {"errors": [{"code": 88}]})])
    with pytest.raises(XError):
        await graphql.search_posts("a", mode="top", limit=5, since=None, until=None,
                                   client=None)
    assert graphql._get_limiter().wait_needed() > 800


async def test_404_refreshes_manifest_once_then_gives_up(layer1, monkeypatch):
    calls = _fake_fetch(monkeypatch, [(404, {}), (200, {"data": _search_data()})])
    res = await graphql.search_posts("a", mode="top", limit=2, since=None, until=None,
                                     client=None)
    assert len(res["results"]) == 2 and len(calls) == 2 and len(layer1) == 2
    _fake_fetch(monkeypatch, [(404, {}), (404, {})])
    with pytest.raises(XError) as e:
        await graphql.search_posts("b", mode="top", limit=2, since=None, until=None,
                                   client=None)
    assert e.value.code == "x_backend_error"


def test_status_is_off_without_cookies(monkeypatch):
    monkeypatch.setattr(config, "X_AUTH_TOKEN", "")
    assert graphql.status() == {"x_account": "off"} and not graphql.enabled()


# --- tool boundary --------------------------------------------------------------------

async def test_x_search_tool_prefers_layer1(app_state, monkeypatch, layer1):
    _fake_fetch(monkeypatch, [(200, {"data": _search_data()})])

    async def no_index(*a, **k):
        raise AssertionError("index must not run when Layer 1 answered")

    monkeypatch.setattr(server, "_x_search_posts", no_index)
    r = await server.x_search("tesla", limit=3)
    assert r["source"] == "graphql" and r["count"] == 3 and r["from_cache"] is False
    assert (await server.x_search("tesla", limit=3))["from_cache"] is True


async def test_x_search_tool_falls_back_to_index_on_account_errors(app_state, monkeypatch,
                                                                   layer1):
    _fake_fetch(monkeypatch, [(200, {"errors": [{"code": 326}]})])

    async def index(query, **kw):
        return {"results": [{"id": "1", "text": "t"}], "source": "index", "degraded": False,
                "degraded_reason": None}

    monkeypatch.setattr(server, "_x_search_posts", index)
    r = await server.x_search("esp32", limit=3)
    assert r["source"] == "index" and r["degraded"] is True
    assert r["degraded_reason"] == "x_account_locked"
    assert SECRET_AUTH not in json.dumps(r) and SECRET_CT0 not in json.dumps(r)
    assert graphql._get_limiter().wait_needed() > 800  # a locked account cools
    # a transient fallback is not cached (cooldown over, e.g. fresh cookies + restart)
    monkeypatch.setattr(graphql, "_limiter", None)
    _fake_fetch(monkeypatch, [(200, {"data": _search_data()})])
    assert (await server.x_search("esp32", limit=3))["source"] == "graphql"


# --- review fixes: refresh floor, single flight, backoff, bounded regex --------------

async def test_forced_refresh_of_a_young_manifest_is_refused(layer1, monkeypatch):
    monkeypatch.setattr(graphql, "_boot", {"gen": xclid.XClIdGen([1] * 48, "k"),
                                           "ops": _ops(), "at": graphql.time.monotonic()})
    _fake_fetch(monkeypatch, [(400, {})])
    with pytest.raises(XError) as e:
        await graphql.search_posts("x" * 400, mode="top", limit=2, since=None, until=None,
                                   client=None)
    assert e.value.code == "x_schema_changed" and layer1 == []  # no page load, no sweep


async def test_bootstrap_is_single_flight(layer1, monkeypatch):
    calls = []

    async def slow_boot(client):
        calls.append(1)
        await graphql.asyncio.sleep(0)
        return {"gen": None, "ops": _ops(), "at": graphql.time.monotonic()}

    monkeypatch.setattr(graphql, "_bootstrap", slow_boot)
    import asyncio
    a, b = await asyncio.gather(graphql._ensure_boot(None), graphql._ensure_boot(None))
    assert a is b and len(calls) == 1


async def test_failed_bootstrap_backs_off_and_cools_the_account(layer1, monkeypatch):
    calls = []

    async def dead(client):
        calls.append(1)
        raise XError("x_account_expired", "cookies not accepted")

    monkeypatch.setattr(graphql, "_bootstrap", dead)
    for _ in range(3):
        with pytest.raises(XError) as e:
            await graphql._ensure_boot(None)
        assert e.value.code == "x_account_expired"
    assert len(calls) == 1 and graphql._get_limiter().wait_needed() > 800


async def test_stale_manifest_survives_an_x_side_failure(layer1, monkeypatch):
    old = {"gen": None, "ops": _ops("OLD" * 5),
           "at": graphql.time.monotonic() - graphql.BOOT_TTL_S - 1}
    monkeypatch.setattr(graphql, "_boot", old)

    async def changed(client):
        raise XError("x_schema_changed", "signing script not found")

    monkeypatch.setattr(graphql, "_bootstrap", changed)
    assert await graphql._ensure_boot(None) is old
    assert await graphql._ensure_boot(None) is old  # backing off, still serving


def test_indices_ref_finds_the_signing_script_without_scanning_everything():
    blob = "A" * 200_000
    bundle = (f'x="{blob}";import("./design.o-1a2b.js");'
              f'import("https://abs.twimg.com/x-web/sign.o-9f8e7d.js");')
    ref = graphql.indices_ref(bundle)  # scheme-relative, as twscrape's regex yields it
    assert graphql.urljoin("https://abs.twimg.com/x-web/main.js", ref) == (
        "https://abs.twimg.com/x-web/sign.o-9f8e7d.js")
    assert graphql.indices_ref(blob) is None


def test_bad_cookie_values_turn_layer1_off(caplog):
    bad = "abc\r\nInjected: 1" + "x" * 20
    assert config._x_cookies(bad, SECRET_CT0) == ("", "")
    assert config._x_cookies(SECRET_AUTH, SECRET_CT0) == (SECRET_AUTH, SECRET_CT0)
    assert config._x_cookies("", "") == ("", "")
    assert SECRET_CT0 not in caplog.text and "Injected" not in caplog.text


async def test_x_search_rejects_huge_queries(app_state):
    r = await server.x_search("a" * 501)
    assert r["code"] == "schema_invalid"


# --- live findings 2026-10-30 ---------------------------------------------------------

def test_page_load_sends_cookie_and_browser_ua_but_no_api_headers(monkeypatch):
    monkeypatch.setattr(config, "X_AUTH_TOKEN", SECRET_AUTH)
    monkeypatch.setattr(config, "X_CT0", SECRET_CT0)
    page = graphql._page_headers()
    assert set(page) == {"user-agent", "cookie"}  # API headers on the page answer 401
    assert "Chrome/" in page["user-agent"] and SECRET_AUTH in page["cookie"]
    assert "Chrome/" in graphql._cookie_headers()["user-agent"]


async def test_bootstrap_finds_signing_script_past_the_bundle_cap(monkeypatch):
    """The logged-in build lists ~900 chunks; the signing script sat at position 540."""
    chunks = "".join(f'{i}:"chunk{i}",' for i in range(900)) + '900:"ondemand.s",'
    hashes = "".join(f'{i}:"{i:016x}",' for i in range(901))
    page = ('<script src="https://abs.twimg.com/responsive-web/client-web/main.abc1234a.js">'
            f"</script><script>{{{chunks}}}{{{hashes}}}</script>")
    fetched = []

    async def fake_page(url, client):
        return page

    async def fake_fetch(url, *, client, timeout=30, **kw):
        fetched.append(url)
        body = ALL_OPS if "/main." in url else "var x=1;" if "ondemand" not in url else "(a[1], 16)"
        return {"final_url": url, "status": 200, "html": body, "render_path": "static"}

    monkeypatch.setattr(graphql, "_page", fake_page)
    monkeypatch.setattr(graphql, "fetch_static", fake_fetch)
    monkeypatch.setattr(graphql.XClIdGen, "from_page",
                        classmethod(lambda cls, p, js: cls([1] * 48, "k")))
    boot = await graphql._bootstrap(None)
    assert boot["ops"]["SearchTimeline"] == ("AbCdEfGhIjKlMnOpQrStUv", {
        "rweb_video_screen_enabled": True, "brand_new_flag": False})
    assert boot["ops"]["UserTweets"][0] == "UsErTwEeTsQuErYiD12345"
    assert "/main." in fetched[0]
    assert any("ondemand.s" in u for u in fetched)  # matched by URL, beyond _MAX_BUNDLES
    assert len(fetched) < 50  # stopped scanning once everything was found


def test_post_text_is_unescaped():
    t = {"rest_id": "1", "legacy": {"full_text": "HRV &amp; cycling &lt;3",
                                    "created_at": "Wed Oct 10 20:19:24 +0000 2018"}}
    assert graphql.post_from_gql(t)["text"] == "HRV & cycling <3"


# --- read through the account: thread, profile, ct0 rotation ----------------------------

def _fixture_data(name: str) -> dict:
    return json.loads((FIX / f"{name}.json").read_text(encoding="utf-8"))["data"]


def test_parse_thread_profile_and_user_posts():
    th = graphql.parse_thread(_fixture_data("gql_tweet_detail"), "1649191520250245121")
    assert th["author"]["handle"] == "XDevelopers" and th["thread"] == []
    assert [p["in_reply_to"] for p in th["replies"]][:1] == ["1649191520250245121"]
    assert len(th["replies"]) == 4
    assert graphql.parse_thread(_fixture_data("gql_tweet_detail"), "1") is None
    user = graphql.parse_user(_fixture_data("gql_user"))
    assert user["handle"] == "XDevelopers" and user["followers"] == 697884
    assert user["id"] == "2244994945" and user["url"] == "https://x.com/XDevelopers"
    assert graphql.parse_user({"user": {"result": {"__typename": "UserUnavailable"}}}) is None
    posts = graphql.parse_user_tweets(_fixture_data("gql_user_tweets"))
    assert len(posts) == 3
    rt = [p for p in posts if p.get("reposted_by")]
    assert len(rt) == 1 and rt[0]["reposted_by"] == "XDevelopers"
    assert rt[0]["author"]["handle"] != "XDevelopers" and not rt[0]["text"].startswith("RT @")


async def test_read_profile_makes_two_calls_and_fetch_renders_it(layer1, monkeypatch):
    await graphql._ensure_boot(None)
    calls = _fake_fetch(monkeypatch, [(200, {"data": _fixture_data("gql_user")}),
                                      (200, {"data": _fixture_data("gql_user_tweets")})])
    res = await graphql.fetch_x_account("https://x.com/XDevelopers", client=None)
    assert ["UserByScreenName" in calls[0]["url"], "UserTweets" in calls[1]["url"]] == [True,
                                                                                        True]
    assert "%222244994945%22" in calls[1]["url"]  # userId from the first answer
    assert res["render_path"] == "api" and res["structured"]["posts"] == 4379
    assert len(res["structured"]["posts_latest"]) == 3 and "<h2>Posts</h2>" in res["html"]


async def test_read_status_renders_thread_and_replies(layer1, monkeypatch):
    await graphql._ensure_boot(None)
    calls = _fake_fetch(monkeypatch, [(200, {"data": _fixture_data("gql_tweet_detail")})])
    res = await graphql.fetch_x_account(
        "https://x.com/XDevelopers/status/1649191520250245121", client=None)
    assert "focalTweetId" in unquote(calls[0]["url"])
    assert "fieldToggles" in calls[0]["url"]
    assert res["structured"]["id"] == "1649191520250245121"
    assert "<h2>Replies</h2>" in res["html"] and "ashevat" in res["html"]


async def test_fetch_x_account_degrades_to_none(layer1, monkeypatch):
    await graphql._ensure_boot(None)
    _fake_fetch(monkeypatch, [(200, {"errors": [{"code": 326}]})])
    assert await graphql.fetch_x_account("https://x.com/a/status/1", client=None) is None
    assert graphql.status()["x_account"] == "locked"
    assert await graphql.fetch_x_account("https://example.com/a", client=None) is None
    monkeypatch.setattr(config, "X_AUTH_TOKEN", "")
    assert await graphql.fetch_x_account("https://x.com/a/status/1", client=None) is None


async def test_fetch_ladder_prefers_the_account_for_x_urls(layer1, monkeypatch):
    from argus.fetch import core

    async def account(url, *, client, timeout=15):
        return {"final_url": url, "status": 200, "html": "<p>t</p>", "render_path": "api",
                "structured": {"id": "1"}}

    async def no_fx(*a, **k):
        raise AssertionError("FxTwitter must not run when the account answered")

    monkeypatch.setattr(core.x_graphql, "fetch_x_account", account)
    monkeypatch.setattr(core, "fetch_x", no_fx)
    res = await core.fetch("https://x.com/a/status/1", client=None)
    assert res["structured"] == {"id": "1"}


ROTATED = "f" * 160


async def test_rotated_ct0_is_used_persisted_and_bound_to_env(layer1, monkeypatch):
    _fake_fetch(monkeypatch, [(200, {"data": _search_data()},
                               [f"ct0={ROTATED}; Max-Age=21600; Path=/; Domain=.x.com"])])
    await graphql.search_posts("a", mode="top", limit=1, since=None, until=None, client=None)
    assert graphql._ct0() == ROTATED and graphql._cookie_headers()["x-csrf-token"] == ROTATED
    saved = graphql.SESSION_PATH.read_text(encoding="utf-8")
    assert ROTATED in saved and SECRET_AUTH not in saved and SECRET_CT0 not in saved
    # a restart picks it up ...
    monkeypatch.setattr(graphql, "_ct0_live", None)
    monkeypatch.setattr(graphql, "_session_loaded", False)
    assert graphql._ct0() == ROTATED
    # ... but freshly copied env cookies win over the old rotation
    monkeypatch.setattr(graphql, "_ct0_live", None)
    monkeypatch.setattr(graphql, "_session_loaded", False)
    monkeypatch.setattr(config, "X_CT0", "NEWENVCT0" + "1" * 20)
    assert graphql._ct0() == "NEWENVCT0" + "1" * 20


async def test_odd_set_cookie_values_are_ignored(layer1, monkeypatch):
    _fake_fetch(monkeypatch, [(200, {"data": _search_data()},
                               ["ct0=short", "other=1", "ct0=bad\r\nvalue" + "x" * 20, 7])])
    await graphql.search_posts("a", mode="top", limit=1, since=None, until=None, client=None)
    assert graphql._ct0() == SECRET_CT0 and not graphql.SESSION_PATH.exists()


# --- second review: read never bootstraps, keeps a budget share, skips protected -------

async def test_read_without_manifest_uses_fxtwitter_and_warms_in_background(layer1,
                                                                            monkeypatch):
    calls = _fake_fetch(monkeypatch, [])
    assert await graphql.fetch_x_account("https://x.com/a/status/1", client=None) is None
    assert calls == []  # no account request on the read path
    await graphql._warm_task
    assert graphql._boot is not None and len(layer1) == 1


async def test_read_stops_at_its_share_of_the_budget(layer1, monkeypatch):
    await graphql._ensure_boot(None)
    lim = graphql._get_limiter()
    now = lim.clock()
    lim.calls.extend([now] * int(lim.per_window * graphql._READ_SHARE))
    calls = _fake_fetch(monkeypatch, [])
    assert await graphql.fetch_x_account("https://x.com/a/status/1", client=None) is None
    assert calls == [] and lim.has_room(0.99)  # x_search still has room


async def test_internal_pacing_does_not_overwrite_account_state(layer1, monkeypatch):
    graphql._get_limiter().cool(60)
    monkeypatch.setattr(graphql, "_state", "ok")
    with pytest.raises(XError) as e:
        await graphql.search_posts("a", mode="top", limit=1, since=None, until=None,
                                   client=None)
    assert e.value.code == "x_rate_limited" and e.value.internal
    assert graphql.status()["x_account"] == "ok"


def test_protected_authors_are_never_returned():
    data = _search_data()
    entries = data["search_by_raw_query"]["search_timeline"]["timeline"]["instructions"][0][
        "entries"]
    user = entries[0]["content"]["itemContent"]["tweet_results"]["result"]["core"][
        "user_results"]["result"]
    user["privacy"] = {"protected": True}
    posts, _ = graphql.parse_search(data)
    assert "Tesla" not in [p["author"]["handle"] for p in posts] and len(posts) == 2
    u = _fixture_data("gql_user")
    u["user"]["result"]["privacy"] = {"protected": True}
    assert graphql.parse_user(u) is None
