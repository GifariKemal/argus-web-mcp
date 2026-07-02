"""Benchmark active Argus MCP tool boundaries with deterministic local fixtures.

This fills the gap left by the quality/search/4-way harnesses: many P2/P3 tools
have integration tests but no repeatable latency/contract benchmark. The script
uses the real ``argus.server`` tool functions, a MockTransport fixture server,
and small fakes only at external API seams.

Usage:
  ./.venv/Scripts/python.exe benchmark/run_tool_surface.py --repeat 3

Outputs JSON + markdown under ``benchmark/_runs/``.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import socket
import statistics
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import fitz
import httpx

from argus import server
from argus.cache import Cache
from argus.watch import WatchStore

ROOT = Path(__file__).resolve().parents[1]
OUT_DIR = ROOT / "benchmark" / "_runs"
BASE = "http://fixtures.test"

ARTICLE_HTML = (
    "<html><head><title>Gold Outlook</title></head><body>"
    "<nav>home about</nav>"
    "<article><h1>Gold Outlook</h1>"
    "<p>" + ("Gold prices are driven by real yields and the dollar. " * 8) + "</p>"
    "<p>" + ("Central bank demand has supported the metal this year. " * 6) + "</p>"
    "</article>"
    "<a href='/page1'>Page 1</a><img src='/gold.png' alt='gold'>"
    "<footer>copyright</footer></body></html>"
)
STRUCT_HTML = (
    "<html><body><h1>Widget</h1>"
    "<span class='price' data-v='9.99'>$9.99</span>"
    "<ul><li>a</li><li>b</li></ul></body></html>"
)
RICH_RENDERED = (
    "<html><body><article>"
    + ("Rendered content sentence here. " * 30)
    + "</article></body></html>"
)


class FakeBrowser:
    def __init__(self, html: str = RICH_RENDERED) -> None:
        self.html = html
        self.calls = 0
        self._crawler = object()

    @property
    def active_contexts(self) -> int:
        return 0

    async def render(
        self,
        url: str,
        *,
        wait_for: str | None = None,
        actions: list | None = None,
        screenshot: bool = False,
        timeout: float = 45,
        stealth: bool = False,
    ) -> dict:
        self.calls += 1
        return {
            "final_url": url,
            "html": self.html,
            "screenshot": "BASE64PNG" if screenshot else None,
            "render_tier": "normal",
        }


@dataclass
class Scenario:
    tool: str
    label: str
    run: Callable[[], Awaitable[dict]]


def _public_dns() -> None:
    def _gai(host: str, port: int, *args: object, **kwargs: object):
        try:
            socket.inet_pton(socket.AF_INET, host)
            ip = host
        except OSError:
            ip = "93.184.216.34"
        return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", (ip, port))]

    socket.getaddrinfo = _gai


def _make_pdf() -> bytes:
    doc = fitz.open()
    page = doc.new_page()
    page.insert_text((72, 72), "Argus PDF benchmark alpha")
    data = doc.tobytes()
    doc.close()
    return data


PDF_BYTES = _make_pdf()


def _mock_client() -> httpx.AsyncClient:
    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if request.method == "POST":
            return httpx.Response(204)
        if path in {"", "/"}:
            return httpx.Response(200, text=ARTICLE_HTML)
        if path == "/article":
            return httpx.Response(200, text=ARTICLE_HTML)
        if path == "/struct":
            return httpx.Response(200, text=STRUCT_HTML)
        if path == "/doc.pdf":
            return httpx.Response(
                200,
                content=PDF_BYTES,
                headers={"content-type": "application/pdf"},
            )
        if path == "/robots.txt":
            return httpx.Response(200, text=f"Sitemap: {BASE}/sitemap.xml\n")
        if path == "/sitemap.xml":
            return httpx.Response(
                200,
                text=(
                    '<?xml version="1.0" encoding="UTF-8"?>'
                    '<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">'
                    f"<url><loc>{BASE}/article</loc></url>"
                    f"<url><loc>{BASE}/struct</loc></url>"
                    "</urlset>"
                ),
            )
        return httpx.Response(404, text="not found")

    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


def _ok(result: dict) -> bool:
    return isinstance(result, dict) and "code" not in result


def _payload_size(result: dict) -> int:
    return len(json.dumps(result, default=str))


async def _timed(scenario: Scenario) -> dict:
    t0 = time.perf_counter()
    try:
        result = await scenario.run()
        error = result.get("code") if isinstance(result, dict) else "non_dict_result"
    except Exception as exc:  # noqa: BLE001 - benchmark should record, not abort
        result = {"error": type(exc).__name__, "code": "benchmark_exception"}
        error = "benchmark_exception"
    elapsed = time.perf_counter() - t0
    return {
        "tool": scenario.tool,
        "label": scenario.label,
        "ok": _ok(result),
        "error": None if _ok(result) else error,
        "latency_ms": round(elapsed * 1000, 3),
        "payload_bytes": _payload_size(result) if isinstance(result, dict) else 0,
        "shape_keys": sorted(result.keys()) if isinstance(result, dict) else [],
    }


def _summarize(records: list[dict]) -> list[dict]:
    rows: list[dict] = []
    groups: dict[tuple[str, str], list[dict]] = {}
    for rec in records:
        groups.setdefault((rec["tool"], rec["label"]), []).append(rec)
    for (tool, label), items in sorted(groups.items()):
        lat = [x["latency_ms"] for x in items]
        payload = [x["payload_bytes"] for x in items]
        p95_idx = max(0, min(len(lat) - 1, round((len(lat) - 1) * 0.95)))
        rows.append(
            {
                "tool": tool,
                "label": label,
                "runs": len(items),
                "ok": sum(1 for x in items if x["ok"]),
                "errors": sorted({x["error"] for x in items if x["error"]}),
                "latency_ms_mean": round(statistics.mean(lat), 3),
                "latency_ms_p95": round(sorted(lat)[p95_idx], 3),
                "payload_bytes_mean": round(statistics.mean(payload), 1),
            }
        )
    return rows


def _render_markdown(rows: list[dict]) -> str:
    lines = [
        "# Argus Tool Surface Benchmark\n\n",
        f"Deterministic fixture benchmark over {len(rows)} active server tool boundaries.\n\n",
        "| tool | label | ok/runs | mean ms | p95 ms | mean bytes | errors |\n",
        "|---|---|---:|---:|---:|---:|---|\n",
    ]
    for row in rows:
        lines.append(
            f"| {row['tool']} | {row['label']} | {row['ok']}/{row['runs']} | "
            f"{row['latency_ms_mean']} | {row['latency_ms_p95']} | "
            f"{row['payload_bytes_mean']} | {', '.join(row['errors']) or '-'} |\n"
        )
    return "".join(lines)


def _patch_external_seams() -> dict[str, Any]:
    originals = {
        "searxng_search": server.searxng_search,
        "_research": server._research,
        "deep_crawl": server.deep_crawl,
        "_gh_search": server._gh_search,
        "_scholar_search": server._scholar_search,
        "_ff_calendar": server._ff_calendar,
        "_cot_report": server._cot_report,
        "_news_feed": server._news_feed,
        "semantic_available": server.semantic.available,
        "semantic_similarities": server.semantic.similarities,
    }

    async def fake_search(query, **kwargs):
        return {
            "query": query,
            "results": [
                {"title": "Python web scraping", "url": f"{BASE}/article",
                 "snippet": "Argus scraping benchmark", "engine": "fixture"},
                {"title": "FastMCP GitHub", "url": "https://github.com/jlowin/fastmcp",
                 "snippet": "FastMCP repository", "engine": "fixture"},
            ],
            "count": 2,
            "engines_used": ["fixture"],
            "backend": "fixture",
            "degraded": False,
            "degraded_reason": None,
        }

    async def fake_research(query, **kwargs):
        return {
            "query": query,
            "mode": kwargs.get("mode", "deep"),
            "sources": [
                {"url": f"{BASE}/article", "title": "Gold Outlook",
                 "content": "Gold benchmark source body.", "word_count": 4,
                 "render_path": "static"}
            ],
            "failed": [],
            "count": 1,
            "source_count_requested": kwargs.get("max_sources", 5),
            "degraded": False,
        }

    async def fake_crawl(seed, **kwargs):
        return {"pages": [{"url": seed, "depth": 0, "content": "crawl body"}],
                "link_graph": {}, "count": 1}

    async def fake_gh(query, **kwargs):
        return {"query": query, "mode": kwargs.get("mode", "repositories"),
                "total_count": 1, "count": 1, "degraded": False,
                "results": [{"full_name": "jlowin/fastmcp", "url": "https://github.com/x"}]}

    async def fake_scholar(query, **kwargs):
        return {"query": query, "source": "fixture", "count": 1,
                "results": [{"title": "Attention Is All You Need", "citations": 100000}]}

    async def fake_ff(date_range=None, client=None):
        return {"events": [{"currency": "USD", "event": "CPI", "impact": "High"}],
                "count": 1, "source": "fixture", "stale": False}

    async def fake_cot(report_type="legacy_futures", date=None, client=None):
        return {"rows": [{"market": "GOLD", "report_date": "2026-01-01"}],
                "count": 1, "report_type": report_type, "source": "fixture",
                "identity_failures": 0, "bad_dates": 0}

    async def fake_news(query, since=None, sentiment=False):
        return {"query": query, "items": [{"title": "Gold rallies", "url": f"{BASE}/article"}],
                "count": 1, "degraded": False}

    server.searxng_search = fake_search
    server._research = fake_research
    server.deep_crawl = fake_crawl
    server._gh_search = fake_gh
    server._scholar_search = fake_scholar
    server._ff_calendar = fake_ff
    server._cot_report = fake_cot
    server._news_feed = fake_news
    server.semantic.available = lambda: True
    server.semantic.similarities = lambda seed, docs: [0.9 - i * 0.1 for i, _ in enumerate(docs)]
    return originals


def _restore_external_seams(originals: dict[str, Any]) -> None:
    server.searxng_search = originals["searxng_search"]
    server._research = originals["_research"]
    server.deep_crawl = originals["deep_crawl"]
    server._gh_search = originals["_gh_search"]
    server._scholar_search = originals["_scholar_search"]
    server._ff_calendar = originals["_ff_calendar"]
    server._cot_report = originals["_cot_report"]
    server._news_feed = originals["_news_feed"]
    server.semantic.available = originals["semantic_available"]
    server.semantic.similarities = originals["semantic_similarities"]


def _scenarios() -> list[Scenario]:
    watch_id: dict[str, str] = {}

    async def register_watch() -> dict:
        out = await server.watch(f"{BASE}/article", "http://webhook.test/hook", 1)
        if "id" in out:
            watch_id["id"] = out["id"]
        return out

    async def unwatch_registered() -> dict:
        if "id" not in watch_id:
            await register_watch()
        return await server.unwatch(watch_id["id"])

    return [
        Scenario("read", "article_media", lambda: server.read(f"{BASE}/article",
                                                              extract_media=True)),
        Scenario("search", "general", lambda: server.search("python scraping", count=2)),
        Scenario(
            "smart_search", "github_route", lambda: server.smart_search("fastmcp github repo")
        ),
        Scenario(
            "smart_search", "science_route",
            lambda: server.smart_search("BM25 vs dense retrieval hybrid search"),
        ),
        Scenario("read_pdf", "text", lambda: server.read_pdf(f"{BASE}/doc.pdf")),
        Scenario("read_pdf", "tables", lambda: server.read_pdf(f"{BASE}/doc.pdf", mode="tables")),
        Scenario("scrape", "browser_screenshot", lambda: server.scrape(f"{BASE}/article",
                                                                        screenshot=True)),
        Scenario("batch_read", "mixed", lambda: server.batch_read([f"{BASE}/article"] * 10,
                                                                  concurrency=4)),
        Scenario("extract_structured", "selector", lambda: server.extract_structured(
            f"{BASE}/struct", {"title": "h1", "price": {"selector": ".price", "attr": "data-v"}}
        )),
        Scenario("crawl", "one_page", lambda: server.crawl(f"{BASE}/article", depth=1,
                                                           max_pages=3)),
        Scenario("screenshot", "browser", lambda: server.screenshot(f"{BASE}/article")),
        Scenario("research", "deep", lambda: server.research("IoT gateway market", max_sources=2)),
        Scenario("map_urls", "sitemap", lambda: server.map_urls(f"{BASE}/", max_urls=10)),
        Scenario("find_similar", "text_seed", lambda: server.find_similar("python web scraping",
                                                                          count=2)),
        Scenario("github_search", "repositories", lambda: server.github_search("fastmcp", limit=2)),
        Scenario("scholar_search", "papers", lambda: server.scholar_search("attention", limit=2)),
        Scenario("watch", "register", register_watch),
        Scenario("list_watches", "list", server.list_watches),
        Scenario("unwatch", "remove", unwatch_registered),
    ]


async def run(repeat: int, concurrency: int = 1) -> tuple[list[dict], list[dict]]:
    _public_dns()
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    old_state = server._S
    originals = _patch_external_seams()
    state = server.State(
        client=_mock_client(),
        cache=Cache(
            db_path=str(OUT_DIR / "tool_surface_cache.db"),
            blob_dir=str(OUT_DIR / "tool_surface_blobs"),
        ),
        browser=FakeBrowser(),
        watch_store=WatchStore(str(OUT_DIR / "tool_surface_watches.json")),
    )
    server._S = state
    try:
        records: list[dict] = []
        workload = [
            (rep, scenario)
            for rep in range(repeat)
            for scenario in _scenarios()
        ]
        if concurrency <= 1:
            for rep, scenario in workload:
                rec = await _timed(scenario)
                rec["rep"] = rep
                records.append(rec)
        else:
            queue: asyncio.Queue[tuple[int, Scenario] | None] = asyncio.Queue()
            for item in workload:
                queue.put_nowait(item)
            for _ in range(concurrency):
                queue.put_nowait(None)

            async def worker() -> None:
                while True:
                    item = await queue.get()
                    try:
                        if item is None:
                            return
                        rep, scenario = item
                        rec = await _timed(scenario)
                        rec["rep"] = rep
                        records.append(rec)
                    finally:
                        queue.task_done()

            await asyncio.gather(*(worker() for _ in range(concurrency)))
        rows = _summarize(records)
        return records, rows
    finally:
        await state.client.aclose()
        state.cache.close()
        server._S = old_state
        _restore_external_seams(originals)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--repeat", type=int, default=3)
    parser.add_argument("--concurrency", type=int, default=1)
    parser.add_argument("--out-prefix", default="tool_surface")
    args = parser.parse_args()

    records, rows = asyncio.run(run(max(1, args.repeat), max(1, args.concurrency)))
    ts = time.strftime("%Y%m%d-%H%M%S")
    json_path = OUT_DIR / f"{args.out_prefix}_{ts}.json"
    md_path = OUT_DIR / f"{args.out_prefix}_{ts}.md"
    json_path.write_text(json.dumps({"records": records, "summary": rows}, indent=2),
                         encoding="utf-8")
    md_path.write_text(_render_markdown(rows), encoding="utf-8")
    print(_render_markdown(rows))
    print(f"\nwrote {json_path}")
    print(f"wrote {md_path}")


if __name__ == "__main__":
    main()
