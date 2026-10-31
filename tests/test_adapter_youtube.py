"""YouTube transcript adapter (Android player API + timedtext) and its place in the ladder."""

import json

import httpx
import pytest
import respx

from argus.extract.article import extract_article
from argus.fetch import adapters
from argus.fetch.adapters import _yt_ts, fetch_youtube, youtube_target
from argus.fetch.core import fetch
from argus.models import STAGE_COUNTS


@pytest.fixture(autouse=True)
def _dns(public_dns):
    """The SSRF guard still resolves every hop; make fixture hosts public."""


VID = "dQw4w9WgXcQ"
YT_URL = f"https://www.youtube.com/watch?v={VID}"
PLAYER = "https://www.youtube.com/youtubei/v1/player"
TT = "https://www.youtube.com/api/timedtext"

FMT1 = ('<?xml version="1.0" encoding="utf-8" ?><transcript>'
        '<text start="0.5" dur="2">Hello &amp;amp; welcome</text>'
        '<text start="10" dur="2">it&amp;#39;s here &lt;script&gt;x()&lt;/script&gt;</text>'
        '<text start="31" dur="2">second paragraph</text>'
        '<text start="3700" dur="2">late cue</text></transcript>')
SRV3 = ('<?xml version="1.0" encoding="utf-8" ?><timedtext format="3"><head>'
        '<pen id="1"/></head><body><p t="1000" d="2000"><s>Hi</s><s> there</s></p>'
        '<p t="40000" d="1000">later on</p><p t="45000" d="500">\n</p></body></timedtext>')


def _track(lang, asr=False):
    t = {"baseUrl": f"{TT}?v={VID}&lang={lang}" + ("&kind=asr" if asr else ""),
         "languageCode": lang, "name": {"simpleText": lang}}
    return {**t, "kind": "asr"} if asr else t


def _player(tracks=None, status="OK", details=True):
    d = {"playabilityStatus": {"status": status}}
    if details:
        d["videoDetails"] = {
            "videoId": VID, "title": "Never <script>t()</script> gonna", "author": "Rick",
            "lengthSeconds": "3725", "viewCount": "1", "channelId": "UC1",
            "shortDescription": "Line one <script>d()</script>\nhttps://x.example/more"}
    if tracks is not None:
        d["captions"] = {"playerCaptionsTracklistRenderer": {"captionTracks": tracks}}
    return d


def _tt(xml):
    return httpx.Response(200, text=xml, headers={"content-type": "text/xml; charset=UTF-8"})


@pytest.mark.parametrize(("url", "expected"), [
    (YT_URL, VID),
    (f"http://youtube.com/watch?v={VID}", VID),
    (f"https://m.youtube.com/watch?feature=share&v={VID}&t=10s", VID),
    (f"https://youtu.be/{VID}", VID),
    (f"https://youtu.be/{VID}/?si=abc", VID),
    (f"https://www.youtube.com/shorts/{VID}", VID),
    (f"https://www.youtube.com/live/{VID}/", VID),
    (f"https://www.youtube.com/embed/{VID}", VID),
    ("https://www.youtube.com/watch?v=short", None),
    (f"https://www.youtube.com/watch?v={VID}x", None),
    ("https://www.youtube.com/watch?v=dQw4w9WgX%3D", None),
    ("https://www.youtube.com/watch", None),
    ("https://www.youtube.com/@channel", None),
    ("https://www.youtube.com/playlist?list=PL123", None),
    (f"https://www.youtube.com/shorts/{VID}/extra", None),
    ("https://youtu.be/", None),
    (f"https://youtube.com.evil.example/watch?v={VID}", None),
    (f"https://evil.example/watch?v={VID}", None),
    (f"ftp://www.youtube.com/watch?v={VID}", None),
])
def test_youtube_target(url, expected):
    assert youtube_target(url) == expected


@pytest.mark.parametrize(("seconds", "expected"), [
    (0, "00:00"), (0.5, "00:00"), (59.9, "00:59"), (61, "01:01"), (3600, "1:00:00"),
    (3725, "1:02:05"), (36000, "10:00:00"),
])
def test_yt_ts(seconds, expected):
    assert _yt_ts(seconds) == expected


@respx.mock
async def test_fetch_youtube_format1_transcript():
    player = respx.post(PLAYER).mock(
        return_value=httpx.Response(200, json=_player([_track("en")])))
    tt = respx.get(TT).mock(return_value=_tt(FMT1))
    async with httpx.AsyncClient() as c:
        res = await fetch_youtube(YT_URL, client=c)
    req = player.calls.last.request
    body = json.loads(req.content)
    assert req.url.params["prettyPrint"] == "false" and body["videoId"] == VID
    assert body["context"]["client"]["clientName"] == "ANDROID"
    assert body["context"]["client"]["clientVersion"] == adapters._YT_CLIENT_VERSION
    assert req.headers["user-agent"].startswith(
        f"com.google.android.youtube/{adapters._YT_CLIENT_VERSION} ")
    assert tt.calls.last.request.url.params["lang"] == "en"
    assert res["render_path"] == "api" and res["status"] == 200 and res["final_url"] == YT_URL
    page = res["html"]
    assert "<script" not in page  # title, description and cue text are all escaped
    text = extract_article(page, url=YT_URL)["content"]
    for want in ("Rick", "1:02:05", "Line one", "https://x.example/more", "Transcript",
                 "[00:00] Hello & welcome it's here <script>x()</script>",
                 "[00:31] second paragraph", "[1:01:40] late cue"):
        assert want in text, want
    assert res["structured"] == {
        "video_id": VID, "title": "Never <script>t()</script> gonna", "channel": "Rick",
        "duration_s": 3725, "language": "en", "auto_generated": False, "languages": ["en"],
        "truncated": False}


@respx.mock
async def test_fetch_youtube_srv3_transcript():
    respx.post(PLAYER).mock(return_value=httpx.Response(200, json=_player([_track("en")])))
    respx.get(TT).mock(return_value=_tt(SRV3))
    async with httpx.AsyncClient() as c:
        res = await fetch_youtube(YT_URL, client=c)
    page = res["html"]
    assert "<p>[00:01] Hi there</p>" in page and "<p>[00:40] later on</p>" in page


@pytest.mark.parametrize(("tracks", "lang", "asr"), [
    ([_track("en", asr=True), _track("de"), _track("en-GB")], "en-GB", False),
    ([_track("en", asr=True), _track("de")], "de", False),
    ([_track("de", asr=True), _track("en", asr=True)], "en", True),
    ([_track("de", asr=True), _track("fr", asr=True)], "de", True),
])
@respx.mock
async def test_fetch_youtube_track_preference(tracks, lang, asr):
    respx.post(PLAYER).mock(return_value=httpx.Response(200, json=_player(tracks)))
    tt = respx.get(TT).mock(return_value=_tt(FMT1))
    async with httpx.AsyncClient() as c:
        res = await fetch_youtube(YT_URL, client=c)
    assert tt.calls.last.request.url.params["lang"] == lang
    s = res["structured"]
    assert s["language"] == lang and s["auto_generated"] is asr
    assert s["languages"] == list(dict.fromkeys(t["languageCode"] for t in tracks))


@pytest.mark.parametrize("player", [_player(None), _player([]),
                                    _player([_track("en")], status="LIVE_STREAM_OFFLINE")])
@respx.mock
async def test_fetch_youtube_without_transcript_keeps_the_record(player):
    respx.post(PLAYER).mock(return_value=httpx.Response(200, json=player))
    tt = respx.get(TT).mock(return_value=_tt(FMT1))
    async with httpx.AsyncClient() as c:
        res = await fetch_youtube(YT_URL, client=c)
    assert tt.call_count == 0
    assert "No transcript available" in res["html"] and "Rick" in res["html"]
    s = res["structured"]
    assert s["language"] is None and s["auto_generated"] is None and s["languages"] == []


@pytest.mark.parametrize("resp", [
    httpx.Response(400, json={"error": {"code": 400}}),
    httpx.Response(403),
    httpx.Response(500),
    httpx.Response(200, text="not json"),
    httpx.Response(200, json=_player([_track("en")], status="LOGIN_REQUIRED")),
    httpx.Response(200, json=_player([_track("en")], status="UNPLAYABLE")),
    httpx.Response(200, json=_player([_track("en")], status="ERROR", details=False)),
    httpx.Response(200, json=_player([_track("en")], details=False)),
    httpx.Response(302, headers={"location": "http://127.0.0.1/internal"}),
    httpx.Response(200, json=_player([{**_track("en"), "baseUrl": "https://evil.example/tt"}])),
    httpx.ConnectError("down"),
])
@respx.mock
async def test_fetch_youtube_player_failure_returns_none(resp):
    respx.post(PLAYER).mock(side_effect=[resp])
    respx.get(TT).mock(return_value=_tt(FMT1))
    internal = respx.route(host="127.0.0.1").mock(return_value=httpx.Response(200))
    evil = respx.route(host="evil.example").mock(return_value=_tt(FMT1))
    async with httpx.AsyncClient() as c:
        assert await fetch_youtube(YT_URL, client=c) is None
    assert internal.call_count == 0 and evil.call_count == 0


@respx.mock
async def test_fetch_youtube_timedtext_failure_returns_none():
    respx.post(PLAYER).mock(return_value=httpx.Response(200, json=_player([_track("en")])))
    respx.get(TT).mock(return_value=httpx.Response(500))
    async with httpx.AsyncClient() as c:
        assert await fetch_youtube(YT_URL, client=c) is None


@respx.mock
async def test_fetch_youtube_rejects_an_oversize_player_answer(monkeypatch):
    from argus.fetch import static

    monkeypatch.setattr(static, "MAX_FETCH_BYTES", 100)
    respx.post(PLAYER).mock(return_value=httpx.Response(200, json=_player([_track("en")])))
    async with httpx.AsyncClient() as c:
        assert await fetch_youtube(YT_URL, client=c) is None


@respx.mock
async def test_fetch_youtube_caps_cues_and_says_so(monkeypatch):
    monkeypatch.setattr(adapters, "_YT_MAX_CUES", 2)
    respx.post(PLAYER).mock(return_value=httpx.Response(200, json=_player([_track("en")])))
    respx.get(TT).mock(return_value=_tt(FMT1))
    async with httpx.AsyncClient() as c:
        res = await fetch_youtube(YT_URL, client=c)
    assert "it&#x27;s here" in res["html"] and "second paragraph" not in res["html"]
    assert "Transcript truncated at 2 cues (2 more not shown)" in res["html"]
    assert res["structured"]["truncated"] is True


async def test_fetch_youtube_non_matching_url_makes_no_request():
    async with httpx.AsyncClient(transport=httpx.MockTransport(lambda r: 1 / 0)) as c:
        assert await fetch_youtube("https://www.youtube.com/@channel", client=c) is None


@respx.mock
async def test_ladder_reads_youtube_from_api_before_the_page():
    page = respx.get(YT_URL).mock(return_value=httpx.Response(200, text="<html>x</html>"))
    respx.post(PLAYER).mock(return_value=httpx.Response(200, json=_player([_track("en")])))
    respx.get(TT).mock(return_value=_tt(FMT1))
    before = STAGE_COUNTS.get("fetch.adapter_ok", 0)
    async with httpx.AsyncClient() as c:
        res = await fetch(YT_URL, client=c, browser=None, timeout=10)
    assert res["render_path"] == "api" and page.call_count == 0
    assert STAGE_COUNTS["fetch.adapter_ok"] == before + 1


@respx.mock
async def test_ladder_falls_back_to_watch_page_when_player_refuses():
    respx.post(PLAYER).mock(return_value=httpx.Response(400))
    respx.get(YT_URL).mock(return_value=httpx.Response(
        200, text="<html><body><article>" + "video text " * 400 + "</article></body></html>"))
    before = STAGE_COUNTS.get("fetch.adapter_fail", 0)
    async with httpx.AsyncClient() as c:
        res = await fetch(YT_URL, client=c, browser=None, timeout=10)
    assert res["render_path"] == "static"
    assert STAGE_COUNTS["fetch.adapter_fail"] == before + 1


def test_cue_cap_is_the_documented_one():
    assert adapters._YT_MAX_CUES == 20_000
