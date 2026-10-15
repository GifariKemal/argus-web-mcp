import asyncio
import json
import re
import sys
from collections import Counter
from urllib.parse import urlsplit

sys.path.insert(0, sys.argv[1] + "/benchmark")
from fastmcp import Client
from fastmcp.client.transports import StreamableHttpTransport
from run_4way import _argus_url_and_token, argus_mcp_config

url, token = _argus_url_and_token(argus_mcp_config())
AUTH = re.compile(r"^(docs?\.|developer\.|learn\.|wiki\.)|(\.gov|\.edu|\.ac\.[a-z]{2}|\.org)$|wikipedia\.org|arxiv\.org|github\.com|gitlab\.com|python\.org|readthedocs\.io|mozilla\.org|microsoft\.com|google\.com|apple\.com|espressif\.com|react\.dev|nextjs\.org|pydata\.org|redis\.io|openssl\.org|pydantic\.dev|stackoverflow\.com|stackexchange\.com|nature\.com|science\.org|ieee\.org|acm\.org|reuters\.com|bloomberg\.com|ft\.com|wsj\.com|nytimes\.com|theguardian\.com|bbc\.co|gartner\.com|mckinsey\.com|iea\.org|nist\.gov|europa\.eu|who\.int|un\.org", re.I)
def host(u): return urlsplit(u).netloc.lower().removeprefix("www.")
queries = json.load(open(sys.argv[2], encoding="utf-8"))
async def main():
    bucket = Counter(); eng = Counter(); eng_tot = Counter(); out = []
    async with Client(StreamableHttpTransport(url, headers={"Authorization": f"Bearer {token}"})) as c:
        for q in queries:
            r = await c.call_tool("search", {"query": q["query"], "count": 30}, raise_on_error=False)
            d = json.loads(r.content[0].text); hits = d.get("results", [])
            for i, h in enumerate(hits):
                a = bool(AUTH.search(host(h["url"]))); b = "1-10" if i < 10 else ("11-20" if i < 20 else "21-30")
                bucket[(b, "auth")] += a; bucket[(b, "n")] += 1
                eng_tot[h.get("engine")] += 1; eng[h.get("engine")] += a
            out.append({"id": q["id"], "n": len(hits), "auth_ranks": [i+1 for i, h in enumerate(hits) if AUTH.search(host(h["url"]))]})
            print(q["id"], len(hits), "auth at ranks", out[-1]["auth_ranks"], flush=True)
    for b in ("1-10", "11-20", "21-30"):
        print(f"ranks {b}: authoritative {bucket[(b,'auth')]}/{bucket[(b,'n')]} ({bucket[(b,'auth')]/max(bucket[(b,'n')],1):.0%})")
    print("by engine (auth/total):", {e: f"{eng[e]}/{eng_tot[e]}" for e in eng_tot})
    json.dump(out, open(sys.argv[3], "w", encoding="utf-8"), indent=1)
asyncio.run(main())
