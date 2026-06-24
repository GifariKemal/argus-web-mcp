"""Link + image extraction from HTML (Firecrawl/Jina parity)."""

from __future__ import annotations

from urllib.parse import urldefrag, urljoin, urlsplit

from parsel import Selector


def _same_host(host: str, base_host: str) -> bool:
    """Suffix match so ``www.`` (or other sub-domain) variants count as same site."""
    return host == base_host or host.endswith("." + base_host) or base_host.endswith("." + host)


def extract_links_images(
    html: str,
    base_url: str,
    *,
    same_domain_only: bool = False,
    max_items: int = 500,
) -> dict:
    """Parse anchors + images from ``html``.

    Returns ``{"links": [{"url", "text"}], "images": [{"src", "alt"}],
    "links_truncated": bool, "images_truncated": bool}``.

    Relative URLs resolve against ``base_url`` (urljoin); link fragments are
    stripped. Links keep only http/https (mailto:/javascript:/tel:/data: dropped);
    images keep http/https plus protocol-relative ``//`` (data: dropped). Each
    list is deduped by absolute url/src (first occurrence, order preserved) and
    capped at ``max_items`` with the matching ``*_truncated`` flag. Never raises
    on malformed HTML - parsel is lenient.
    """
    sel = Selector(text=html or "")
    base_host = urlsplit(base_url).hostname or ""

    links: list[dict] = []
    seen_links: set[str] = set()
    for node in sel.css("a"):
        href = (node.attrib.get("href") or "").strip()
        if not href:
            continue
        url = urldefrag(urljoin(base_url, href)).url
        if urlsplit(url).scheme not in ("http", "https") or url in seen_links:
            continue
        if same_domain_only and not _same_host(urlsplit(url).hostname or "", base_host):
            continue
        seen_links.add(url)
        links.append({"url": url, "text": (node.xpath("string(.)").get() or "").strip()})

    images: list[dict] = []
    seen_imgs: set[str] = set()
    for node in sel.css("img"):
        s = (node.attrib.get("src") or "").strip()
        if not s or s.startswith("data:"):
            continue
        src_abs = urljoin(base_url, s)
        if urlsplit(src_abs).scheme not in ("http", "https") or src_abs in seen_imgs:
            continue
        seen_imgs.add(src_abs)
        images.append({"src": src_abs, "alt": node.attrib.get("alt", "")})

    return {
        "links": links[:max_items],
        "images": images[:max_items],
        "links_truncated": len(links) > max_items,
        "images_truncated": len(images) > max_items,
    }
