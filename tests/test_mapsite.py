import socket

import httpx
import pytest

from argus.mapsite import MapError, map_site
from argus.security.ssrf import SSRFError


def _gai(mapping):
    def _f(host, port, *a, **k):
        ip = mapping.get(host, "93.184.216.34")
        return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", (ip, port))]

    return _f


def _client(handler):
    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


def _sitemap(locs):
    body = "".join(f"<url><loc>{u}</loc></url>" for u in locs)
    return (
        '<?xml version="1.0" encoding="UTF-8"?>'
        '<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">'
        f"{body}</urlset>"
    )


def _sitemap_index(sitemaps):
    body = "".join(f"<sitemap><loc>{u}</loc></sitemap>" for u in sitemaps)
    return (
        '<?xml version="1.0" encoding="UTF-8"?>'
        '<sitemapindex xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">'
        f"{body}</sitemapindex>"
    )


async def test_robots_points_to_sitemap(monkeypatch):
    monkeypatch.setattr(socket, "getaddrinfo", _gai({}))
    locs = ["https://x.test/a", "https://x.test/b"]

    def h(req):
        p = req.url.path
        if p == "/robots.txt":
            return httpx.Response(200, text="User-agent: *\nSitemap: https://x.test/sitemap.xml\n")
        if p == "/sitemap.xml":
            return httpx.Response(200, text=_sitemap(locs))
        return httpx.Response(404)

    async with _client(h) as c:
        res = await map_site("https://x.test/", client=c)
    assert set(res["urls"]) == set(locs)
    assert "sitemap" in res["source"]
    assert res["count"] == 2
    assert res["truncated"] is False


async def test_direct_sitemap(monkeypatch):
    monkeypatch.setattr(socket, "getaddrinfo", _gai({}))
    locs = ["https://x.test/1", "https://x.test/2", "https://x.test/3"]

    def h(req):
        p = req.url.path
        if p == "/robots.txt":
            return httpx.Response(404)
        if p == "/sitemap.xml":
            return httpx.Response(200, text=_sitemap(locs))
        return httpx.Response(404)

    async with _client(h) as c:
        res = await map_site("https://x.test/", client=c)
    assert set(res["urls"]) == set(locs)
    assert res["count"] == 3
    assert res["source"] == "sitemap"


async def test_sitemap_index_merges_children(monkeypatch):
    monkeypatch.setattr(socket, "getaddrinfo", _gai({}))

    def h(req):
        p = req.url.path
        if p == "/robots.txt":
            return httpx.Response(404)
        if p == "/sitemap.xml":
            return httpx.Response(
                200,
                text=_sitemap_index(
                    ["https://x.test/sm1.xml", "https://x.test/sm2.xml"]
                ),
            )
        if p == "/sm1.xml":
            return httpx.Response(200, text=_sitemap(["https://x.test/a", "https://x.test/b"]))
        if p == "/sm2.xml":
            return httpx.Response(200, text=_sitemap(["https://x.test/c"]))
        return httpx.Response(404)

    async with _client(h) as c:
        res = await map_site("https://x.test/", client=c)
    assert set(res["urls"]) == {
        "https://x.test/a",
        "https://x.test/b",
        "https://x.test/c",
    }
    assert res["count"] == 3


async def test_sitemap_index_child_cap(monkeypatch):
    monkeypatch.setattr(socket, "getaddrinfo", _gai({}))
    children = [f"https://x.test/sm{i}.xml" for i in range(15)]

    def h(req):
        p = req.url.path
        if p == "/robots.txt":
            return httpx.Response(404)
        if p == "/sitemap.xml":
            return httpx.Response(200, text=_sitemap_index(children))
        # each child sitemap yields one loc named after its own path
        return httpx.Response(200, text=_sitemap([f"https://x.test{p}-loc"]))

    async with _client(h) as c:
        res = await map_site("https://x.test/", client=c)
    # only the first 10 child sitemaps are fetched (cap), so 10 locs.
    assert res["count"] == 10


async def test_fallback_links_same_site_only(monkeypatch):
    monkeypatch.setattr(socket, "getaddrinfo", _gai({}))
    html = (
        "<html><body>"
        '<a href="/page1">p1</a>'
        '<a href="https://x.test/page2">p2</a>'
        '<a href="https://other.test/x">off</a>'
        '<a href="mailto:a@x.test">mail</a>'
        "</body></html>"
    )

    def h(req):
        p = req.url.path
        if p in ("/robots.txt", "/sitemap.xml"):
            return httpx.Response(404)
        return httpx.Response(200, text=html)

    async with _client(h) as c:
        res = await map_site("https://x.test/", client=c)
    assert res["source"] == "links"
    assert "https://x.test/page1" in res["urls"]
    assert "https://x.test/page2" in res["urls"]
    assert all("other.test" not in u for u in res["urls"])
    assert all(not u.startswith("mailto:") for u in res["urls"])


async def test_max_urls_cap_truncates(monkeypatch):
    monkeypatch.setattr(socket, "getaddrinfo", _gai({}))
    locs = [f"https://x.test/p{i}" for i in range(10)]

    def h(req):
        p = req.url.path
        if p == "/robots.txt":
            return httpx.Response(404)
        if p == "/sitemap.xml":
            return httpx.Response(200, text=_sitemap(locs))
        return httpx.Response(404)

    async with _client(h) as c:
        res = await map_site("https://x.test/", client=c, max_urls=4)
    assert res["count"] == 4
    assert len(res["urls"]) == 4
    assert res["truncated"] is True


async def test_subdomains_included(monkeypatch):
    monkeypatch.setattr(socket, "getaddrinfo", _gai({}))
    locs = ["https://x.test/a", "https://blog.x.test/b", "https://other.test/c"]

    def h(req):
        p = req.url.path
        if p == "/robots.txt":
            return httpx.Response(404)
        if p == "/sitemap.xml":
            return httpx.Response(200, text=_sitemap(locs))
        return httpx.Response(404)

    async with _client(h) as c:
        res = await map_site("https://x.test/", client=c, include_subdomains=True)
    assert "https://x.test/a" in res["urls"]
    assert "https://blog.x.test/b" in res["urls"]
    assert all("other.test" not in u for u in res["urls"])


async def test_subdomains_excluded(monkeypatch):
    monkeypatch.setattr(socket, "getaddrinfo", _gai({}))
    locs = ["https://x.test/a", "https://blog.x.test/b"]

    def h(req):
        p = req.url.path
        if p == "/robots.txt":
            return httpx.Response(404)
        if p == "/sitemap.xml":
            return httpx.Response(200, text=_sitemap(locs))
        return httpx.Response(404)

    async with _client(h) as c:
        res = await map_site("https://x.test/", client=c, include_subdomains=False)
    assert "https://x.test/a" in res["urls"]
    assert all("blog.x.test" not in u for u in res["urls"])


async def test_ssrf_seed_blocked(monkeypatch):
    monkeypatch.setattr(
        socket, "getaddrinfo", _gai({"169.254.169.254": "169.254.169.254"})
    )
    with pytest.raises(SSRFError):
        await map_site("http://169.254.169.254/", client=None)


async def test_nothing_discoverable_raises(monkeypatch):
    monkeypatch.setattr(socket, "getaddrinfo", _gai({}))

    def h(req):
        return httpx.Response(404)

    async with _client(h) as c:
        with pytest.raises(MapError) as ei:
            await map_site("https://x.test/", client=c)
    assert ei.value.code == "fetch_failed"


async def test_bad_scheme_loc_skipped(monkeypatch):
    monkeypatch.setattr(socket, "getaddrinfo", _gai({}))
    locs = ["https://x.test/ok", "javascript:void(0)", "ftp://x.test/file"]

    def h(req):
        p = req.url.path
        if p == "/robots.txt":
            return httpx.Response(404)
        if p == "/sitemap.xml":
            return httpx.Response(200, text=_sitemap(locs))
        return httpx.Response(404)

    async with _client(h) as c:
        res = await map_site("https://x.test/", client=c)
    assert res["urls"] == ["https://x.test/ok"]
    assert res["source"] == "sitemap"


async def test_empty_sitemap_falls_through_to_links(monkeypatch):
    monkeypatch.setattr(socket, "getaddrinfo", _gai({}))

    def h(req):
        p = req.url.path
        if p == "/robots.txt":
            return httpx.Response(404)
        if p == "/sitemap.xml":
            return httpx.Response(200, text=_sitemap([]))
        return httpx.Response(200, text='<a href="/only">o</a>')

    async with _client(h) as c:
        res = await map_site("https://x.test/", client=c)
    assert res["source"] == "links"
    assert res["urls"] == ["https://x.test/only"]


async def test_github_io_subdomains_are_separate_registrable_domains(monkeypatch):
    # github.io is a user-namespace public suffix: a.github.io and b.github.io are
    # DIFFERENT sites, so b.github.io must be out of scope even with include_subdomains.
    monkeypatch.setattr(socket, "getaddrinfo", _gai({}))
    locs = ["https://a.github.io/p1", "https://b.github.io/p2"]

    def h(req):
        p = req.url.path
        if p == "/robots.txt":
            return httpx.Response(404)
        if p == "/sitemap.xml":
            return httpx.Response(200, text=_sitemap(locs))
        return httpx.Response(404)

    async with _client(h) as c:
        res = await map_site("https://a.github.io/", client=c, include_subdomains=True)
    assert "https://a.github.io/p1" in res["urls"]
    assert all("b.github.io" not in u for u in res["urls"])


async def test_client_none_builds_and_closes_default_client(monkeypatch):
    monkeypatch.setattr(socket, "getaddrinfo", _gai({}))
    locs = ["https://x.test/a", "https://x.test/b"]

    def h(req):
        p = req.url.path
        if p == "/robots.txt":
            return httpx.Response(404)
        if p == "/sitemap.xml":
            return httpx.Response(200, text=_sitemap(locs))
        return httpx.Response(404)

    built = httpx.AsyncClient(transport=httpx.MockTransport(h))
    rec = {"built": 0}

    def fake_build(**kw):
        rec["built"] += 1
        return built

    import argus.mapsite as mapsite_mod

    monkeypatch.setattr(mapsite_mod, "build_safe_async_client", fake_build)

    res = await map_site("https://x.test/", client=None)  # no client passed
    assert rec["built"] == 1
    assert set(res["urls"]) == set(locs)
    assert built.is_closed  # default client was closed in the finally


async def test_malformed_sitemap_falls_through_to_links(monkeypatch):
    monkeypatch.setattr(socket, "getaddrinfo", _gai({}))

    def h(req):
        p = req.url.path
        if p == "/robots.txt":
            return httpx.Response(404)
        if p == "/sitemap.xml":
            return httpx.Response(200, text="<urlset><loc>not closed properly")
        return httpx.Response(200, text='<a href="/recovered">r</a>')

    async with _client(h) as c:
        res = await map_site("https://x.test/", client=c)
    assert res["source"] == "links"
    assert "https://x.test/recovered" in res["urls"]


# --- gzipped sitemaps (.xml.gz payloads) ---------------------------------------


async def test_gzipped_sitemap_is_decompressed(monkeypatch):
    import gzip

    monkeypatch.setattr(socket, "getaddrinfo", _gai({}))
    locs = [f"https://x.test/p{i}" for i in range(100)]
    gz_body = gzip.compress(_sitemap(locs).encode("utf-8"))

    def h(req):
        p = req.url.path
        if p == "/robots.txt":
            return httpx.Response(
                200, text="Sitemap: https://x.test/sitemap.xml.gz\n"
            )
        if p == "/sitemap.xml.gz":
            return httpx.Response(
                200, content=gz_body, headers={"content-type": "application/gzip"}
            )
        return httpx.Response(404)

    async with _client(h) as c:
        res = await map_site("https://x.test/", client=c)
    assert res["count"] == 100
    assert res["source"] == "robots+sitemap"


async def test_gz_url_already_decompressed_still_parses(monkeypatch):
    """httpx may have un-gzipped via Content-Encoding: a .gz URL serving plain XML must
    still parse (magic sniff, not URL suffix)."""
    monkeypatch.setattr(socket, "getaddrinfo", _gai({}))

    def h(req):
        p = req.url.path
        if p == "/robots.txt":
            return httpx.Response(200, text="Sitemap: https://x.test/sitemap.xml.gz\n")
        if p == "/sitemap.xml.gz":
            return httpx.Response(200, text=_sitemap(["https://x.test/only"]))
        return httpx.Response(404)

    async with _client(h) as c:
        res = await map_site("https://x.test/", client=c)
    assert res["urls"] == ["https://x.test/only"]


async def test_corrupt_gzip_sitemap_skipped_not_fatal(monkeypatch):
    monkeypatch.setattr(socket, "getaddrinfo", _gai({}))

    def h(req):
        p = req.url.path
        if p == "/robots.txt":
            return httpx.Response(200, text="Sitemap: https://x.test/sitemap.xml.gz\n")
        if p == "/sitemap.xml.gz":
            return httpx.Response(200, content=b"\x1f\x8btruncated-garbage")
        if p == "/":
            return httpx.Response(200, text='<a href="https://x.test/fallback">f</a>')
        return httpx.Response(404)

    async with _client(h) as c:
        res = await map_site("https://x.test/", client=c)
    assert res["source"] == "links"  # degraded to link fallback, no crash


async def test_sitemap_transport_error_falls_back_to_links(monkeypatch):
    monkeypatch.setattr(socket, "getaddrinfo", _gai({}))

    def h(req):
        p = req.url.path
        if p == "/robots.txt":
            return httpx.Response(200, text="Sitemap: https://x.test/sitemap.xml\n")
        if p == "/sitemap.xml":
            raise httpx.ConnectError("refused")
        if p == "/":
            return httpx.Response(200, text='<a href="https://x.test/fb">f</a>')
        return httpx.Response(404)

    async with _client(h) as c:
        res = await map_site("https://x.test/", client=c)
    assert res["source"] == "links"


async def test_gzip_bomb_sitemap_skipped(monkeypatch):
    """A sitemap whose DECOMPRESSED size exceeds the fetch cap must be skipped, not
    ballooned into memory."""
    import gzip

    from argus.fetch import static as static_mod

    monkeypatch.setattr(socket, "getaddrinfo", _gai({}))
    monkeypatch.setattr(static_mod, "MAX_FETCH_BYTES", 1024)
    import argus.mapsite as mapsite_mod

    monkeypatch.setattr(mapsite_mod, "MAX_FETCH_BYTES", 1024)
    bomb = gzip.compress(b"<urlset>" + b"x" * 10_000 + b"</urlset>")

    def h(req):
        p = req.url.path
        if p == "/robots.txt":
            return httpx.Response(200, text="Sitemap: https://x.test/sitemap.xml.gz\n")
        if p == "/sitemap.xml.gz":
            return httpx.Response(200, content=bomb)
        if p == "/":
            return httpx.Response(200, text='<a href="https://x.test/fb">f</a>')
        return httpx.Response(404)

    async with _client(h) as c:
        res = await map_site("https://x.test/", client=c)
    assert res["source"] == "links"  # bomb skipped, discovery degraded gracefully
