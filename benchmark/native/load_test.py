"""Multi-user load on the live VPS: N concurrent clients each running research(deep) and
read on distinct queries/URLs; latency per level. usage: load_test.py <repo> <compare24.json> <out.json>"""
import asyncio
import json
import statistics as st
import sys
import time

sys.path.insert(0, sys.argv[1] + "/benchmark")
from fastmcp import Client  # noqa: E402
from fastmcp.client.transports import StreamableHttpTransport  # noqa: E402
from run_4way import _argus_url_and_token, argus_mcp_config  # noqa: E402

url, token = _argus_url_and_token(argus_mcp_config())
queries = json.load(open(sys.argv[2], encoding="utf-8"))
READS = ["https://peps.python.org/pep-0008/", "https://docs.python.org/3/library/asyncio-task.html",
         "https://fastapi.tiangolo.com/tutorial/first-steps/", "https://en.wikipedia.org/wiki/Modbus",
         "https://www.paulgraham.com/greatwork.html", "https://developer.mozilla.org/en-US/docs/Web/JavaScript/Reference/Global_Objects/Promise",
         "https://docs.espressif.com/projects/esp-idf/en/stable/esp32/api-reference/peripherals/uart.html",
         "https://react.dev/learn/thinking-in-react"]


async def one_client(i, level, ts):
    """One simulated agent: a research(deep) then a read, both cache-busted."""
    out = []
    async with Client(StreamableHttpTransport(url, headers={"Authorization": f"Bearer {token}"})) as c:
        q = queries[(level * 7 + i) % len(queries)]["query"] + f" {ts}"  # new query text = cache miss
        t = time.perf_counter()
        r = await asyncio.wait_for(c.call_tool("research", {"query": q, "mode": "deep", "max_sources": 5}, raise_on_error=False), 200)
        d = json.loads(r.content[0].text)
        out.append({"tool": "research", "secs": round(time.perf_counter() - t, 1), "n": len(d.get("sources", [])), "error": d.get("error")})
        u = READS[(level * 3 + i) % len(READS)] + f"?lt={ts}{i}"
        t = time.perf_counter()
        r = await asyncio.wait_for(c.call_tool("read", {"url": u, "format": "text"}, raise_on_error=False), 120)
        d = json.loads(r.content[0].text)
        out.append({"tool": "read", "secs": round(time.perf_counter() - t, 1), "words": (d.get("metadata") or {}).get("word_count"), "error": d.get("error")})
    return out


async def main():
    results = {}
    for level in (1, 3, 5, 8):
        ts = int(time.time())
        t = time.perf_counter()
        per = await asyncio.gather(*(one_client(i, level, ts) for i in range(level)), return_exceptions=True)
        wall = round(time.perf_counter() - t, 1)
        flat = [x for p in per if isinstance(p, list) for x in p]
        errs = [repr(p)[:80] for p in per if not isinstance(p, list)]
        rs = [x["secs"] for x in flat if x["tool"] == "research"]
        rd = [x["secs"] for x in flat if x["tool"] == "read"]
        results[level] = {"wall": wall, "research": rs, "read": rd, "errors": errs + [x["error"] for x in flat if x["error"]]}
        print(f"level {level}: wall {wall}s | research p50 {st.median(rs) if rs else None} max {max(rs) if rs else None} | read p50 {st.median(rd) if rd else None} max {max(rd) if rd else None} | errors {len(results[level]['errors'])}", flush=True)
        await asyncio.sleep(10)
    json.dump(results, open(sys.argv[3], "w", encoding="utf-8"), indent=1)

asyncio.run(main())
