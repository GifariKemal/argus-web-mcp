"""Telegram public channels (t.me/s/ preview) as one record per post."""

import httpx
import pytest
import respx

from argus.extract.article import extract_article
from argus.fetch.adapters import fetch_telegram, telegram_target
from argus.fetch.core import fetch
from argus.models import STAGE_COUNTS


@pytest.fixture(autouse=True)
def _dns(public_dns):
    """The SSRF guard still resolves every hop; make fixture hosts public."""


def _msg(post, text, when, views, media=False):
    body = (f'<div class="tgme_widget_message_text js-message_text" dir="auto">{text}</div>'
            if text is not None else "")
    extra = '<a class="tgme_widget_message_photo_wrap"></a>' if media else ""
    return (f'<div class="tgme_widget_message_wrap js-widget_message_wrap">'
            f'<div class="tgme_widget_message text_not_supported_wrap js-widget_message" '
            f'data-post="{post}"><div class="tgme_widget_message_bubble">{extra}{body}'
            f'<div class="tgme_widget_message_footer"><div class="tgme_widget_message_info">'
            f'<span class="tgme_widget_message_views">{views}</span>'
            f'<span class="tgme_widget_message_meta"><a class="tgme_widget_message_date" '
            f'href="https://t.me/{post}"><time datetime="{when}" class="time">20:45</time>'
            f'</a></span></div></div></div></div></div>')


PAGE = ("<html><head><meta property=\"og:title\" content=\"Gold Desk\"></head><body>"
        '<div class="tgme_channel_info_header_title"><span>Gold Desk</span></div>'
        + _msg("golddesk/10", "XAUUSD <b>long</b> above 3900<br/>SL 3880",
               "2026-10-30T08:00:00+00:00", "12.4K")
        + _msg("golddesk/11", 'Read <a href="https://ex.test/report">the report</a> '
               "&lt;script&gt;x&lt;/script&gt;", "2026-10-31T01:00:00+00:00", "980")
        + _msg("golddesk/12", None, "2026-10-31T02:00:00+00:00", "1.1K", media=True)
        + "</body></html>")


@pytest.mark.parametrize(("url", "expected"), [
    ("https://t.me/s/golddesk", "https://t.me/s/golddesk"),
    ("https://t.me/golddesk", "https://t.me/s/golddesk"),
    ("http://telegram.me/golddesk/", "https://t.me/s/golddesk"),
    ("https://t.me/s/golddesk?before=50", "https://t.me/s/golddesk?before=50"),
    ("https://t.me/golddesk/11", "https://t.me/golddesk/11?embed=1&mode=tme"),
    ("https://t.me/s/golddesk/11", "https://t.me/golddesk/11?embed=1&mode=tme"),
    ("https://t.me/joinchat/AbCdEf", None),
    ("https://t.me/+AbCdEfGh", None),
    ("https://t.me/addstickers/pack", None),
    ("https://t.me/c/123/45", None),
    ("https://t.me/ab", None),  # usernames are 4+ characters here (5+ on Telegram)
    ("https://t.me/s/golddesk?before=x", "https://t.me/s/golddesk"),
    ("https://t.me.evil.example/golddesk", None),
    ("https://evil.example/s/golddesk", None),
    ("ftp://t.me/golddesk", None),
])
def test_telegram_target(url, expected):
    assert telegram_target(url) == expected


@respx.mock
async def test_fetch_telegram_one_record_per_post_newest_first():
    respx.get("https://t.me/s/golddesk").mock(return_value=httpx.Response(200, text=PAGE))
    async with httpx.AsyncClient() as c:
        res = await fetch_telegram("https://t.me/golddesk", client=c)
    assert res["render_path"] == "api" and res["final_url"] == "https://t.me/golddesk"
    s = res["structured"]
    assert s["channel"] == "golddesk" and s["title"] == "Gold Desk"
    assert [p["id"] for p in s["posts"]] == [12, 11, 10]
    first = s["posts"][2]
    assert first == {"id": 10, "url": "https://t.me/golddesk/10",
                     "date": "2026-10-30T08:00:00+00:00", "views": "12.4K",
                     "text": "XAUUSD long above 3900\nSL 3880", "media": False}
    assert s["posts"][0]["text"] == "" and s["posts"][0]["media"] is True
    # escaped markup in a post stays text, never live HTML in the page we build
    assert "<script>" not in res["html"] and "&lt;script&gt;" in res["html"]
    art = extract_article(res["html"], url="https://t.me/golddesk")
    assert "XAUUSD long above 3900" in art["content"] and "VIEW IN TELEGRAM" not in art["content"]
    assert "https://t.me/golddesk/11" in res["html"]


@respx.mock
@pytest.mark.parametrize("resp", [httpx.Response(404), httpx.Response(200, text="<html></html>")])
async def test_fetch_telegram_no_posts_or_error_returns_none(resp):
    respx.get("https://t.me/s/golddesk").mock(return_value=resp)
    async with httpx.AsyncClient() as c:
        assert await fetch_telegram("https://t.me/s/golddesk", client=c) is None


async def test_fetch_telegram_ignores_other_hosts():
    assert await fetch_telegram("https://ex.test/s/golddesk", client=None) is None


@respx.mock
async def test_read_max_chars_trims_the_posts_with_the_content(app_state, monkeypatch):
    from argus import server

    respx.get("https://t.me/s/golddesk").mock(return_value=httpx.Response(200, text=PAGE))
    async with httpx.AsyncClient() as c:
        res = await fetch_telegram("https://t.me/golddesk", client=c)

    async def fake_fetch(url, **kw):  # app_state's client serves fixtures only
        return res

    monkeypatch.setattr(server, "fetch", fake_fetch)
    full = await server.read("https://t.me/golddesk")
    posts = full["metadata"]["structured"]["posts"]
    assert len(posts) == 3
    cut_at = full["content"].index(posts[1]["url"]) + len(posts[1]["url"])
    capped = await server.read("https://t.me/golddesk", max_chars=cut_at)
    assert [p["id"] for p in capped["metadata"]["structured"]["posts"]] == [12, 11]
    assert capped["metadata"]["structured"]["older_url"] == full["metadata"]["structured"][
        "older_url"]
    tiny = await server.read("https://t.me/golddesk", max_chars=5)  # no link in the cut
    assert len(tiny["metadata"]["structured"]["posts"]) == 3


@respx.mock
async def test_ladder_reads_telegram_through_the_adapter():
    respx.get("https://t.me/s/golddesk").mock(return_value=httpx.Response(200, text=PAGE))
    before = STAGE_COUNTS.get("fetch.adapter_ok", 0)
    async with httpx.AsyncClient() as c:
        res = await fetch("https://t.me/golddesk", client=c, browser=None, timeout=10)
    assert res["render_path"] == "api" and len(res["structured"]["posts"]) == 3
    assert STAGE_COUNTS["fetch.adapter_ok"] == before + 1
