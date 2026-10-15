"""Functional + latency check of the tools the main benchmark did not cover, on the live
VPS: smart_search routing, map_urls, github_search, scholar_search, find_similar, crawl,
batch_read. usage: tools_bench.py <repo> <out.json>"""
import asyncio
import json
import sys
import time

sys.path.insert(0, sys.argv[1] + "/benchmark")
from fastmcp import Client  # noqa: E402
from fastmcp.client.transports import StreamableHttpTransport  # noqa: E402
from run_4way import _argus_url_and_token, argus_mcp_config  # noqa: E402

url, token = _argus_url_and_token(argus_mcp_config())
ROUTES = [  # (query, expected route)
    ("fastmcp python library github repository", "github"),
    ("attention is all you need transformer paper arxiv", "scholar"),
    ("EU AI act news this week", "news"),
    ("python asyncio gather exception handling stackoverflow", "it"),
    ("ESP32 brownout detector reset", "general"),
    ("protein folding alphafold 3 peer reviewed study", "science"),
    ("best modbus gateway vendors", "general"),
    ("react 19 release notes github", "github"),
]
CALLS = [
    ("map_urls", {"url": "https://fastapi.tiangolo.com/", "max_urls": 200}),
    ("github_search", {"query": "esp-idf modbus gateway", "mode": "repositories", "limit": 5}),
    ("scholar_search", {"query": "scaled dot product attention", "limit": 5}),
    ("find_similar", {"url_or_text": "https://docs.python.org/3/library/asyncio-task.html", "count": 5}),
    ("crawl", {"seed_url": "https://peps.python.org/pep-0020/", "max_pages": 3, "depth": 1}),
    ("batch_read", {"urls": ["https://peps.python.org/pep-0008/", "https://peps.python.org/pep-0257/",
                             "https://peps.python.org/pep-0484/", "https://peps.python.org/pep-0001/"]}),
]


async def call(c, name, args):
    t = time.perf_counter()
    try:
        r = await asyncio.wait_for(c.call_tool(name, args, raise_on_error=False), 180)
        d = json.loads(r.content[0].text)
    except Exception as e:  # noqa: BLE001
        d = {"error": f"client:{e!r}"}
    return d, round(time.perf_counter() - t, 2)


def summarize(name, d):
    if d.get("error"):
        return f"ERROR {d.get('code')} {str(d.get('error'))[:80]}"
    if name == "map_urls":
        return f"{d.get('count') or len(d.get('urls', []))} urls, source {d.get('source') or d.get('sources')}"
    if name in ("github_search", "scholar_search"):
        rs = d.get("results") or d.get("items") or []
        return f"{len(rs)} results, first: {str((rs[0] if rs else {}).get('title') or (rs[0] if rs else {}).get('name', ''))[:60]}"
    if name == "find_similar":
        rs = d.get("results") or d.get("similar") or []
        return f"{len(rs)} similar, first: {str((rs[0] if rs else {}).get('url', ''))[:70]}"
    if name == "crawl":
        pg = d.get("pages") or []
        return f"{len(pg)} pages, words {sum(p.get('word_count', 0) for p in pg)}"
    if name == "batch_read":
        return f"succeeded {d.get('succeeded')} failed {d.get('failed')}"
    return str(list(d)[:6])


async def main():
    out = {"routes": [], "calls": []}
    async with Client(StreamableHttpTransport(url, headers={"Authorization": f"Bearer {token}"})) as c:
        for q, want in ROUTES:
            d, dt = await call(c, "smart_search", {"query": q, "count": 5})
            got = d.get("route")
            n = len(((d.get("result") or {}).get("results")) or ((d.get("result") or {}).get("items")) or [])
            out["routes"].append({"query": q, "want": want, "got": got, "n": n, "secs": dt, "error": d.get("error")})
            print(f"smart_search {'OK ' if got == want else 'DIFF'} want={want:8s} got={str(got):8s} n={n:2d} {dt}s  {q}", flush=True)
        for name, args in CALLS:
            d, dt = await call(c, name, args)
            out["calls"].append({"tool": name, "args": args, "secs": dt, "summary": summarize(name, d), "error": d.get("error")})
            print(f"{name:14s} {dt:6.1f}s  {summarize(name, d)}", flush=True)
    json.dump(out, open(sys.argv[2], "w", encoding="utf-8"), indent=1)

asyncio.run(main())
