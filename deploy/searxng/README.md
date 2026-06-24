# SearXNG - Argus search backend

Self-hosted, unlimited search engine that Argus' `search` tool queries over its
JSON API. Bound to **loopback only** (`127.0.0.1:8888`) - it is an internal
dependency, never exposed to the public internet.

## 1. Set the secret key (required before first run)

`server.secret_key` in `settings.yml` ships as the placeholder
`CHANGE_ME_GENERATE_RANDOM`. Replace it with a real random value:

```bash
sed -i "s/CHANGE_ME_GENERATE_RANDOM/$(openssl rand -hex 32)/" settings.yml
```

## 2. Start

```bash
docker compose up -d
```

## 3. Verify the JSON API

```bash
curl 'http://127.0.0.1:8888/search?q=test&format=json'
```

Expect a JSON body with a `results` array. If you get HTML or a 403 instead,
confirm `search.formats` in `settings.yml` includes `json` and restart:
`docker compose restart`.

## Notes

- **Loopback binding** is enforced by the compose port mapping
  `127.0.0.1:8888:8080`. Do not change the `127.0.0.1` prefix - without it the
  instance would be reachable from the network.
- `limiter: false` because the only client is Argus (a trusted local process).
  Re-enable it if the port is ever exposed beyond loopback.
- Argus talks to it via `base_url="http://127.0.0.1:8888"` in `argus.search`.

## Reliability - engines + proxy pool (IMPORTANT)

Validated live 2026-06-24: on a **single IP**, the big free engines (brave/google/
duckduckgo/startpage) rate-limit/CAPTCHA after a short burst -> SearXNG returns 0 results
with `unresponsive_engines` set (Argus surfaces this as a **retryable** `search_backend_down`,
not `no_results`). Two mitigations are configured in `settings.yml`:

1. **Broadened engine set** (already enabled): low-friction engines like **bing** and
   **mojeek** keep answering when the majors are throttled - verified returning 10-20
   results/query while brave/google/ddg were all suspended. No cost.
2. **Outbound proxy pool** (`outgoing.proxies`, commented template): route SearXNG's engine
   requests through rotating proxies so the majors don't throttle by IP. Datacenter IPs (the
   VPS) throttle FASTER than residential, so this matters most in production. To enable:
   uncomment the `outgoing.proxies` block and fill with your real `socks5h://`/`http://`
   proxy endpoints, then `docker compose restart`. Keep proxy creds out of git (inject at
   deploy via your secret manager / `$ARGUS_SEARXNG_PROXIES`).

Tip: confirm which engines answered with
`curl '.../search?q=x&format=json' | jq '.results[].engine, .unresponsive_engines'`.
