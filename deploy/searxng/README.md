# SearXNG - Argus search backend

Self-hosted, unlimited search engine that Argus' `search` tool queries over its JSON API.
It is an internal dependency and is never exposed to the public internet on any path.

> **Status: LIVE.** SearXNG runs in production on the SURIOTA VPS (`43.134.17.144`) as the
> search backend for `https://argus.gifariksuryo.xyz/mcp`.

## Which compose file runs it

| Path | File | How Argus reaches it |
|---|---|---|
| **Production (Easypanel)** and local dev | the repo root `docker-compose.yml` | `http://searxng:8080` over the compose network. **No host port is published at all**, so there is nothing to firewall. |
| Panel-less fallback (`provision.sh`, systemd) | the `docker-compose.yml` in *this* folder | `http://127.0.0.1:8888`, loopback-only port mapping |

Both mount `settings.yml` from this folder, so engine configuration is shared. The rest of
this page applies to either.

## Contents

- [1. Set the secret key](#1-set-the-secret-key-required-before-first-run)
- [2. Start](#2-start)
- [3. Verify the JSON API](#3-verify-the-json-api)
- [Notes](#notes)
- [Engines - measure, never assume](#engines---measure-never-assume)

## 1. Set the secret key (required before first run)

> [!IMPORTANT]
> Do this before the first start. The placeholder `CHANGE_ME_GENERATE_RANDOM` is public in this repo - starting with it leaves a known secret key.

`server.secret_key` in `settings.yml` ships as the placeholder
`CHANGE_ME_GENERATE_RANDOM`. Replace it with a real random value:

```bash
sed -i "s/CHANGE_ME_GENERATE_RANDOM/$(openssl rand -hex 32)/" settings.yml
```

On the VPS the value comes from the `SEARXNG_SECRET` service env instead, which overrides
the file, so the placeholder there is harmless.

## 2. Start

```bash
# with Argus (the normal way)
docker compose -f docker-compose.yml -f docker-compose.local.yml up -d

# standalone, panel-less path only
cd deploy/searxng && docker compose up -d
```

## 3. Verify the JSON API

```bash
# from the Argus container (compose network)
docker exec argus_argus-argus-1 python -c \
  "import urllib.request;print(urllib.request.urlopen('http://searxng:8080/search?q=test&format=json').status)"

# standalone path
curl 'http://127.0.0.1:8888/search?q=test&format=json'
```

Expect a JSON body with a `results` array. If you get HTML or a 403 instead, confirm
`search.formats` in `settings.yml` includes `json` and restart the container.

## Notes

> [!WARNING]
> A mounted file's **content** changing does not make compose recreate the container, so a
> deploy that only edits `settings.yml` leaves the old config loaded in memory. Restart it
> by hand: `docker restart argus_argus-searxng-1`.

- `limiter: false` because the only client is Argus, reaching it over a private network
  with no published port. Re-enable it if a port is ever exposed.
- Argus sends an `X-Real-IP` header on every request: SearXNG assumes it sits behind a
  reverse proxy and logs an error per process without one. An `X-Forwarded-For nor
  X-Real-IP` error in the log therefore comes from a hand-rolled probe script, not Argus.

## Engines - measure, never assume

Which engines answer is a property of **the host's IP**, not of this configuration. Free
engines block or CAPTCHA whole datacenter ranges and the block differs per provider, so
every claim here was measured from the VPS rather than reasoned about.

```bash
python scripts/check_engines.py --url http://searxng:8080
```

The checker counts only results **attributed to the engine asked for** and exits non-zero
when one answers nothing, so it doubles as a post-deploy gate. That attribution check
matters: SearXNG silently discards an `engines=` name it does not recognise and answers
from its default set instead, so a total-result count scores a nonexistent engine as
healthy on another engine's work. That is exactly how `startpage` and `marginalia`, which
this image does not ship at all, passed as healthy for months.

Measured from this VPS on 2026-09-18:

| Category | Answering | Disabled, and why |
|---|---|---|
| `general` | `bing`, `brave`, `google`, `google cse`, `duckduckgo web`, `yandex` | `mojeek`, `duckduckgo`, `qwant` (IP-blocked, pointed at the dormant `proxied` network); `startpage`, `marginalia` (not shipped upstream); `wikidata` (times out) |
| `news` | `brave.news`, `duckduckgo news`, `google news`, `bing news`, `wikinews` | `reuters` (HTTP error) |
| `science` | `arxiv`, `europepmc`, `semantic scholar`, `pdbe` | `google scholar` (access denied, on the `proxied` network), `pubmed` (engine crashes), `openairepublications`, `openairedatasets` (5 s timeout each) |
| `it` | `github`, `stackoverflow`, `docker hub`, `mdn`, `askubuntu`, `superuser`, `hoogle`, `mankier` | `gentoo` (connection error), `pypi`, `arch linux wiki` (silently empty) |

Two traps worth remembering:

- **Only `general` is driven by `ARGUS_SEARCH_ENGINES`.** `news`, `science` and `it` send no
  `engines=` at all, so SearXNG runs its whole default set for the category and the only
  lever is `disabled: true` in `settings.yml`.
- **A zero from a narrow index proves nothing.** `hoogle`, `mankier` and `pdbe` return
  nothing for a general query and answer fine for `fmap`, `tar` and `hemoglobin`. Probe a
  narrow engine with something it should actually know before calling it dead.

### Proxy path for the IP-blocked engines

`outgoing.networks.proxied` in `settings.yml` points the IP-blocked engines at a compose
service named `proxy`, so no provider URL or credential ever lands in this repo;
`docker-compose.proxy.yml` runs that service from `RESIDENTIAL_PROXY_URL`. With no proxy
running, those engines simply stay disabled and cost nothing.

It buys less than it used to: DuckDuckGo coverage came back through `duckduckgo web`
without any proxy, so the remaining prize is `mojeek`, `qwant` and `google scholar`. Free
options are exhausted - a free datacenter proxy measured **worse** than the VPS's own IP,
and Cloudflare WARP still drew CAPTCHAs because its range is a known VPN.
