# Argus VPS Deployment Runbook

> [!IMPORTANT]
> **Since 2026-09-15 production runs on Easypanel, not on the systemd units below.**
> Read [Easypanel deployment (current)](#easypanel-deployment-current) first. The
> systemd + nginx + certbot + fail2ban recipe that follows it is still supported
> and still the right answer for a box without a panel, but on
> `43.134.17.144` those units are installed-and-disabled.

> **Status: DEPLOYED-LIVE.** Argus runs in production at
> `https://argus.gifariksuryo.xyz/mcp` on the SURIOTA VPS `43.134.17.144`, as an
> Easypanel Compose service built from the repo's own `docker-compose.yml`.
> `/health` returns 200; `/mcp` returns 401 without a bearer token.

## Easypanel deployment (current)

[Easypanel](https://easypanel.io/) owns the host's edge: it installs Docker,
initialises Swarm, runs **Traefik** on `:80`/`:443` (Let's Encrypt included), and
serves its own panel on `:3000`. Argus is a **Compose** service, not an App
service, for one concrete reason: Chromium needs more than the default 64 MB of
`/dev/shm`, and `shm_size` is a Compose setting that Docker Swarm does not
support.

| Piece | Value |
|---|---|
| Project / service | `argus` / `argus` |
| Source | git `https://github.com/GifariKemal/argus-web-mcp.git`, ref `main`, compose file `docker-compose.yml` |
| Containers | `argus` (uvicorn `:8090`) and `searxng` (reached at `http://searxng:8080` over the compose network); Docker names them `argus_argus-argus-1` and `argus_argus-searxng-1` |
| Domain | `argus.gifariksuryo.xyz` -> service `argus`, port `8090`, HTTPS via Traefik |
| Service env | `ARGUS_TOKEN` (bearer), `SEARXNG_SECRET` (overrides `server.secret_key`), `ARGUS_SEARCH_ENGINES` (`bing,brave,google cse` - engines this image HAS and this IP can reach; verify a name against `/config` before adding it, because SearXNG drops an unknown one silently) |
| Auto-deploy | GitHub push webhook -> Easypanel deploy URL -> rebuild + restart |

Everything is drivable over the panel's REST API (`http://127.0.0.1:3000/api`,
`Authorization: Bearer <api token>`; the full OpenAPI spec is served at
`/api/openapi.json`). Useful calls:

```bash
# health of the deployed stack
curl -s -H "Authorization: Bearer $EP_TOKEN" \
  "http://127.0.0.1:3000/api/inspectComposeService?projectName=argus&serviceName=argus"

# redeploy by hand (the push webhook does this for you)
curl -s -X POST -H "Authorization: Bearer $EP_TOKEN" -H "Content-Type: application/json" \
  -d '{"projectName":"argus","serviceName":"argus"}' \
  http://127.0.0.1:3000/api/deployComposeService

# container-level view
sudo docker compose -p argus_argus ps
sudo docker compose -p argus_argus logs --tail 50 argus

# SearXNG reads settings.yml and limiter.toml through a bind mount, and compose does
# not recreate a container when a mounted file's CONTENT changes - a deploy that only
# touches those files leaves the old config loaded in memory. Restart it by hand:
sudo docker restart argus_argus-searxng-1
```

The panel itself is at `https://panel.gifariksuryo.xyz` (Traefik-issued Let's
Encrypt cert), and the GitHub webhook posts to that host so the deploy token in
its path never crosses the wire in clear text.

> [!WARNING]
> **`updateComposeEnv` turns off `.env` generation unless you resend
> `createDotEnv: true`.** Omit it and the panel still shows the new variable while
> compose interpolates `${VAR}` to an empty string, with no error anywhere. And compose
> only passes variables the service *declares*: a key in `.env` reaches the container
> only if `docker-compose.yml` lists it under `environment:`. Both bit this deployment.

> [!NOTE]
> `setPanelDomain{"serveOnIp": false}` does not actually stop the panel from
> answering on the raw IP, so `:3000` is dropped from the internet by iptables
> rules in both `INPUT` and `DOCKER-USER` (saved with `iptables-persistent`).
> `http://127.0.0.1:3000/api` over SSH stays available as the fallback.

### Traefik middlewares (what replaced nginx + fail2ban)

| Middleware | Type | Attached to | Why |
|---|---|---|---|
| `argus-cloudflare-only` | ipAllowList (the 22 published Cloudflare ranges) | `argus.gifariksuryo.xyz/` | The DNS record is proxied by Cloudflare, but the origin stayed reachable by IP, so the edge could simply be skipped. Traefik now answers 403 to anything that did not come through Cloudflare. Scoped to this router, so the hostnames that resolve straight to the origin (`*.easypanel.host`, `*.sslip.io`) are untouched. |
| `argus-ratelimit` | rateLimit, 300/60s, burst 100, keyed on `CF-Connecting-IP` | `argus.gifariksuryo.xyz/` | Brute-force protection the fail2ban jail used to give. Behind Cloudflare every request arrives from an edge IP, so keying on the remote address put all clients in one bucket; `CF-Connecting-IP` restores per-client counting and is trustworthy because the gate above rejects anyone who bypassed the edge. Measured: a 30-way parallel burst of 400 gets 249 rejections. |
| `argus-internal-only` | ipAllowList (loopback, host, docker ranges) | `argus.gifariksuryo.xyz/metrics` | `/metrics` was loopback-only under nginx; routing the whole host to Argus published it. Traefik matches the longer path first, so this rule wins for `/metrics` alone. |

> [!IMPORTANT]
> Middlewares live in Easypanel's database (`/etc/easypanel/data/data.mdb`) and
> `traefik/config/main.yaml` is generated from it, so edit them through the panel API
> (`createMiddleware`, `updateMiddleware`, `updateDomain`) and never by hand in that file.
> `zz-panel-hardening.yaml` in the same directory is the exception: Easypanel only writes
> `main.yaml`, so a separate file survives.

> [!NOTE]
> Easypanel's **metrics retention and log collection need a paid licence** (`A license
> with advanced monitoring support is required`), and creating a notification channel
> returns 200 on the free tier but stores nothing. Container logs and `docker stats` cover
> the same ground - `/metrics` is still scraped from inside the host.

### The Cloudflare layer (what it does and does not do)

`argus.gifariksuryo.xyz` is **proxied** by Cloudflare (orange cloud), as is every other
`gifariksuryo.xyz` hostname. Three things are worth knowing before reasoning about it.

**It is an inbound proxy only.** It does nothing for the search engines that block this
host: the egress IP is still the VPS's own, and `duckduckgo`, `qwant` and `mojeek` keep
failing with `proxy error` because they point at the `proxy` compose service, which is
not running. Measured after the record was proxied, not assumed.

**Long tool calls survive the edge.** Cloudflare's free plan drops an origin connection
after 100 s of silence, which would have cut `crawl` (180 s bound) and deep `research`.
It does not, because the MCP transport is `text/event-stream` and FastMCP sends a
`: ping` comment every ~15 s, so the stream is never idle. A 180 s crawl returns
`http=200 time=180.1s` through Cloudflare.

**Certificate renewal is unaffected.** Traefik answers the ACME HTTP-01 challenge from
its internal `acme-http` router, which carries none of the middlewares above, and
Cloudflare passes `/.well-known/acme-challenge/` through instead of redirecting it
(a probe returns 404 from Traefik, not a 301). The edge serves its own Cloudflare
certificate; the origin keeps its Let's Encrypt one, so `Full (strict)` is safe.

The origin was **not** firewalled to the Cloudflare ranges, even though that is the usual
advice. Port 80 has to stay open to the world for the ACME challenges of
`47vspn.easypanel.host`, `traefik.47vspn.easypanel.host` and
`zonelab.43.134.17.144.sslip.io`, which resolve straight to the IP by design. Blocking at
the host would have broken their renewals silently, three months later. The
`argus-cloudflare-only` middleware achieves the same thing for the one router that needs
it, with no collateral.

### Search engines and the proxy path

Which free engines answer is a property of the host's IP, so it is configuration, not
code: `ARGUS_SEARCH_ENGINES` picks the fan-out. Measure the current host with

```bash
python scripts/check_engines.py --url http://searxng:8080
```

It exits non-zero when an engine returns nothing, so it also works as a post-deploy gate.

`settings.yml` defines an `outgoing.networks.proxied` network and points the blocked
engines (`mojeek`, `duckduckgo`, `qwant`) at it. The target is the compose service
`proxy`, never a provider URL, so no credential is ever committed:
`docker-compose.proxy.yml` runs that service and reads `RESIDENTIAL_PROXY_URL` from the
service env. **Cloudflare WARP was measured on 2026-09-15 and does not work** - its
egress range is itself a known VPN, so duckduckgo still CAPTCHAs and qwant and mojeek
still return access denied. Only a provider with residential exit IPs will change that.

**Rolling back to systemd:** stop the stack (`sudo docker compose -p argus_argus down`,
or use the panel), then
`sudo systemctl enable --now argus nginx argus-update.timer certbot.timer`.
The units, the nginx vhost and the certbot cert are all still on disk.

## Systemd deployment (panel-less alternative)

The rest of this document provisions Argus directly on a host with
`deploy/provision.sh`: uvicorn `127.0.0.1:8090 --workers 1`, SearXNG docker
`127.0.0.1:8888`, nginx + Let's Encrypt TLS, fail2ban, and the
poll-and-health-gate auto-update timer. The steps were executed for the original
deploy and remain the re-provision recipe. The examples use
`argus.gifariksuryo.xyz` (the live host).

## Contents

- [Easypanel deployment (current)](#easypanel-deployment-current)
- [Systemd deployment (panel-less alternative)](#systemd-deployment-panel-less-alternative)

- [Overview](#overview)
- [Architecture](#architecture)
- [Deployment Files](#deployment-files)
- [Prerequisites](#prerequisites)
- [Step 1: Copy Files to VPS](#step-1-copy-files-to-vps)
- [Step 2: Update Domain Placeholder](#step-2-update-domain-placeholder)
- [Step 3: Run Provision Script](#step-3-run-provision-script)
- [Step 4: Retrieve Bearer Token](#step-4-retrieve-bearer-token)
- [Step 5: Verify the Deployment](#step-5-verify-the-deployment)
- [Step 6: Register in Claude Code (Client Side)](#step-6-register-in-claude-code-client-side)
- [Step 7: Set Up Hermes Monitoring (Optional)](#step-7-set-up-hermes-monitoring-optional)
- [Rollback / Recovery](#rollback--recovery)
- [Security Checklist](#security-checklist)
- [Environment Variables (Optional Tuning)](#environment-variables-optional-tuning)
- [Port Map Summary](#port-map-summary)
- [Troubleshooting](#troubleshooting)
- [References](#references)
- [Safe auto-update](#safe-auto-update-poll-main---health-check---auto-rollback)
- [Contact & Support](#contact--support)

## Overview

This directory contains the **configuration files** that provision Argus on the SURIOTA VPS (`43.134.17.144`, Ubuntu 24.04). Argus binds to `127.0.0.1:8090` locally, and nginx proxies the public HTTPS subdomain to it. The repo lives at `/opt/argus/app`, its venv at `/opt/argus/app/.venv`, and the runtime caches at `/opt/argus/.argus`, `/opt/argus/.cache`, `/opt/argus/.crawl4ai` - `argus.service` and `argus-update.sh` hardcode those paths.

The P1+P2 exit gates passed and the service is live (see [Roadmap](../docs/02-ROADMAP.md)). Re-run the steps below to re-provision or to stand up a second instance; tests must be green locally first.

## Architecture

<p align="center">
  <img src="../assets/architecture.svg" alt="Argus deploy topology: CLI over HTTPS bearer to nginx (TLS, fail2ban), proxied to uvicorn+FastMCP on 127.0.0.1:8090, with SearXNG docker on 127.0.0.1:8888" width="100%">
</p>

Request flow: **Claude Code CLI** hits nginx over HTTPS with a bearer token. nginx (`argus.<domain>`, TLS, fail2ban) proxies `/mcp` to uvicorn on `127.0.0.1:8090`; `/health` is unauthenticated for monitoring and `/metrics` is Prometheus (optional IP allowlist). SearXNG runs as its own Docker container on `127.0.0.1:8888` (loopback-only JSON API), reached by Argus via an httpx client.

| Layer | Detail |
|---|---|
| Systemd service | `argus.service` (`User=argus`, `EnvironmentFile=/etc/argus/argus.env`) |
| Auth | Bearer token (`StaticTokenVerifier` -> `JWTVerifier`) |
| TLS | certbot (Let's Encrypt, auto-renewal) |
| Rate limit | fail2ban (401 brute-force protection on `/mcp`) |

## Deployment Files

| File | Purpose |
|---|---|
| `argus.service` | systemd unit (uvicorn, unprivileged user, hardening) |
| `argus.env.example` | Environment template (copy to `/etc/argus/argus.env`) |
| `argus.nginx.conf` | nginx server block (TLS, streaming, auth) |
| `fail2ban-argus.conf` | fail2ban jail (401 brute-force protection) |
| `provision.sh` | Idempotent bash script (run as root) |
| `searxng/` | SearXNG docker-compose (already present, do not modify) |
| `argus-update.sh` | Safe auto-update: poll main, ff-only, health-gate, auto-rollback |
| `argus-update.service` | Oneshot unit that runs `argus-update.sh` (root) |
| `argus-update.timer` | Polls main every 5 min to trigger the update |

## Prerequisites

> [!IMPORTANT]
> The domain in `argus.nginx.conf` and `provision.sh` must be a real subdomain (not the `argus.<domain>` placeholder) before you run certbot, or TLS issuance fails.

Before running deployment, ensure:

1. **VPS Access**: SSH key-only to `43.134.17.144` as user `ubuntu`
   ```bash
   ssh -i ~/.ssh/gifari_vps_ed25519 ubuntu@43.134.17.144
   ```

2. **Root Privileges**: The provision script runs as root
   ```bash
   sudo su -
   ```

3. **Domain**: Have your actual subdomain ready (e.g., `argus.gifariksuryo.xyz`)
   - Replace placeholder `argus.<domain>` in `argus.nginx.conf` before running certbot
   - TLS certificate must exist (certbot will generate)

4. **Port Availability**: Confirm ports are free on the VPS
   - `:8090` for Argus (local, should be free)
   - `:8888` for SearXNG (already in use by P1, expected)

5. **Git Repository**: Argus code is cloned from GitHub
   - Update `ARGUS_REPO` in `provision.sh` if using a private/fork URL
   - Ensure the branch exists on that repo

## Step 1: Copy Files to VPS

Copy the `deploy/` directory to the VPS (as the `ai` user first, then provision.sh moves them):

```bash
# From your local machine
scp -r -i ~/.ssh/gifari_vps_ed25519 deploy/ ubuntu@43.134.17.144:/tmp/argus-deploy

# On the VPS, as root
sudo cp -r /tmp/argus-deploy /opt/argus-deploy-staging
```

Or, if Argus is already cloned:
```bash
# On VPS, as root
cd /opt/argus
# (already has deploy/ in the repo)
```

## Step 2: Update Domain Placeholder

Before running `provision.sh`, replace `argus.<domain>` with your actual domain in both files:

```bash
# On the VPS, as root
sudo sed -i 's/argus\.<domain>/argus.gifariksuryo.xyz/g' /opt/argus-deploy-staging/argus.nginx.conf
sudo sed -i 's/argus\.<domain>/argus.gifariksuryo.xyz/g' /opt/argus-deploy-staging/provision.sh
```

Or, manually edit:
```bash
sudo nano /opt/argus-deploy-staging/argus.nginx.conf
```

## Step 3: Run Provision Script

```bash
# On VPS, as root
cd /opt/argus  # Or wherever you staged the files
sudo bash deploy/provision.sh

# This script is idempotent - safe to re-run if it fails.
```

> [!NOTE]
> `provision.sh` is idempotent - re-run it if a step fails.

<details><summary>What the provision script does (14 steps)</summary>

1. Updates system packages (`apt update && apt upgrade`)
2. Installs system deps (Python 3.12, nginx, certbot, fail2ban, Docker)
3. Creates unprivileged `argus` user + `/opt/argus` home
4. Clones the Argus repository from GitHub
5. Sets up Python venv + installs dependencies (via `uv`)
6. Installs browser binaries (Playwright/Crawl4AI) **as the `argus` user** (critical for cache)
7. Starts SearXNG Docker container (via `docker-compose`)
8. Creates `/etc/argus/` directory and generates fresh `ARGUS_TOKEN` (saved to `/etc/argus/argus.env`)
9. Installs systemd service (`argus.service`)
10. Installs nginx config + enables the site
11. Installs fail2ban jail + filter
12. Runs certbot to generate TLS certificate (requires domain to not be a placeholder)
13. Starts the Argus service
14. Verifies `/health` endpoint responds

</details>

**Expected output** at the end:
```
========== Provisioning Complete [x] ==========
...
Service Status:
  systemctl status argus
...
Bearer Token (save this somewhere safe):
  ARGUS_TOKEN=a1b2c3d4e5f6a7b8...xxxx (truncated)
...
```

## Step 4: Retrieve Bearer Token

> [!IMPORTANT]
> The bearer token is a secret. Store it in a password manager. Never commit it to git or paste it into a tracked file.

The token is printed at the end of `provision.sh`. Save it externally (password manager, not in git):

```bash
# On VPS, as root (if you missed it above)
cat /etc/argus/argus.env | grep ARGUS_TOKEN
```

## Step 5: Verify the Deployment

```bash
# On VPS

# 1. Check service status
systemctl status argus
# Expected: active (running)

# 2. Check logs
journalctl -u argus -n 50
# Should show successful startup

# 3. Test health endpoint locally (no auth)
curl http://127.0.0.1:8090/health
# Expected: {"status": "ok", "browser_alive": true}

# 4. Test via nginx + TLS (with auth)
ARGUS_TOKEN="<token-from-step-4>"
curl https://argus.gifariksuryo.xyz/health \
  -H "Authorization: Bearer $ARGUS_TOKEN"
# Expected: same JSON response

# 5. Check SearXNG
docker compose -f /opt/searxng ps
# Expected: argus-searxng container running

# 6. Check fail2ban
fail2ban-client status argus
# Expected: "Filter: nginx-argus-bearer-401" + "0 bans" (initially)
```

## Step 6: Register in Claude Code (Client Side)

On your local machine, register the MCP in Claude Code:

```bash
# Substitute your actual ARGUS_TOKEN
export ARGUS_TOKEN="<token-from-step-4>"

claude mcp add --transport http argus \
  https://argus.gifariksuryo.xyz/mcp \
  --header "Authorization: Bearer $ARGUS_TOKEN"
```

This writes to `~/.claude/mcp.json` (or `.claude/settings.json`):
```json
{
  "argus": {
    "transport": "http",
    "url": "https://argus.gifariksuryo.xyz/mcp",
    "headers": {
      "Authorization": "Bearer <ARGUS_TOKEN>"
    }
  }
}
```

Verify zero local process:
```bash
# On your local machine
ps aux | grep argus
# Should return nothing (no local server, pure HTTP remote)
```

## Step 7: Set Up Hermes Monitoring (Optional)

The Hermes watchdog can monitor Argus health every 30 minutes:

```bash
# On VPS, add to Hermes crontab or watchdog config
*/30 * * * * curl -s https://argus.gifariksuryo.xyz/health \
  -H "Authorization: Bearer $ARGUS_TOKEN" \
  | jq -e '.status == "ok"' > /dev/null || alert

# If the check fails, trigger an alert (Telegram, email, etc.)
```

Or configure Prometheus to scrape `/metrics`:

```yaml
# In /etc/prometheus/prometheus.yml
scrape_configs:
  - job_name: 'argus'
    bearer_token: '<ARGUS_TOKEN>'
    static_configs:
      - targets: ['https://argus.gifariksuryo.xyz:443/metrics']
```

## Rollback / Recovery

If something goes wrong:

### Service won't start

```bash
journalctl -u argus -n 100
# Check the error, then either:
# 1. Fix the config
systemctl restart argus

# 2. Or, temporarily stop and debug
systemctl stop argus
/opt/argus/.venv/bin/uvicorn argus.server:app --host 127.0.0.1 --port 8090
# (Run in foreground to see errors)
```

### Regenerate bearer token

> [!CAUTION]
> Regenerating the token invalidates every existing client. The overwrite of `/etc/argus/argus.env` also drops any other env vars in that file - re-add them after. Every user must re-register in Claude Code with the new token.

```bash
# On VPS, as root
sudo bash -c "echo 'ARGUS_TOKEN=$(openssl rand -hex 32)' > /etc/argus/argus.env"
sudo chmod 600 /etc/argus/argus.env
systemctl restart argus

# Get the new token
cat /etc/argus/argus.env | grep ARGUS_TOKEN

# Notify all users to update their Claude Code registration
```

### TLS certificate expired or needs renewal

```bash
certbot renew --force-renewal
systemctl reload nginx
```

Certbot should auto-renew 30 days before expiry (via systemd timer).

### Rollback to previous version

> [!WARNING]
> `git checkout <previous-commit>` puts the repo in a detached HEAD state; the safe auto-update timer polls `main` and can ff it forward again. Pause the timer (`systemctl disable --now argus-update.timer`) if you need the rollback to hold.

```bash
cd /opt/argus
git log --oneline
git checkout <previous-commit>
# Re-install dependencies (if dependencies changed)
./.venv/bin/uv pip install -e .
systemctl restart argus
```

### IP banned by fail2ban

If your office IP gets banned after repeated 401 errors (typos, stale token):

```bash
fail2ban-client set argus unbanip <YOUR_IP>
# Then re-register in Claude Code with the correct token
```

## Security Checklist

- [x] Service runs as unprivileged `argus` user (no root escalation)
- [x] Bearer token stored in `/etc/argus/argus.env` (0600, root-only)
- [x] TLS via certbot (auto-renew, A+ rating)
- [x] nginx `proxy_buffering off` (safe streaming MCP)
- [x] fail2ban limits brute-force on `/mcp` (401 rate limit)
- [x] SSRF hardened in Argus server code (100% test coverage, DNS resolution + private-IP deny + re-pin)
- [x] Coexists with Hermes/SUVA (separate ports, no collision)
- [x] Browser pool runs as `argus` user (correct cache ownership)

## Environment Variables (Optional Tuning)

In `/etc/argus/argus.env`, you can optionally set:

```bash
# LLM integration (for extract_structured LLM tier)
ARGUS_LLM_API_KEY=<key>
ARGUS_LLM_BASE_URL=https://api.groq.com/openai/v1
ARGUS_LLM_MODEL=mixtral-8x7b-32768

# Browser concurrency (default 4, increase for throughput)
ARGUS_MAX_CONCURRENT_CONTEXTS=8

# Per-tool timeouts (seconds): ARGUS_TIMEOUT_<TOOL> (see config.py), e.g.
ARGUS_TIMEOUT_READ=60
ARGUS_TIMEOUT_CRAWL=180

# Logging level
ARGUS_LOG_LEVEL=INFO  # or DEBUG for verbose logs
```

Then restart:
```bash
systemctl restart argus
journalctl -u argus -f  # Verify new settings
```

## Port Map Summary

| Port | Service | Binding | Public? |
|---|---|---|---|
| 80 | nginx redirect | 0.0.0.0:80 | Yes (-> 443) |
| 443 | nginx HTTPS (Argus) | 0.0.0.0:443 | Yes (TLS) |
| 8090 | Argus uvicorn | 127.0.0.1:8090 | No (local) |
| 8888 | SearXNG Docker | 127.0.0.1:8888 | No (local) |
| 8080 | SUVA | 127.0.0.1:8080 | No (local, coexist) |
| (80 also) | Hermes | 0.0.0.0:80 | Yes (coexist via nginx SNI/host routing) |

> [!WARNING]
> **Port 80/443 conflict.** Hermes and Argus both want port 80/443. The provision script does NOT modify Hermes - you must ensure your nginx upstream config multiplexes both via SNI or Host header routing. See the Hermes deployment guide for how to add Argus as a second upstream block.

## Troubleshooting

### `/mcp` endpoint returns 401

Check the bearer token:
```bash
curl -i https://argus.gifariksuryo.xyz/mcp
# Should return 401 (no Authorization header)

ARGUS_TOKEN="..."
curl -i https://argus.gifariksuryo.xyz/mcp \
  -H "Authorization: Bearer $ARGUS_TOKEN"
# Should return 200 (or a streaming response)
```

### Service crashes with "Cannot open display" or IPC error

The browser cache is owned by the wrong user. Verify:
```bash
ls -la /opt/argus/.cache/ms-playwright/
# Should be owned by argus:argus, not root:root
```

Fix:
```bash
sudo chown -R argus:argus /opt/argus/.cache
systemctl restart argus
```

### SearXNG not responding

```bash
docker compose -f /opt/searxng ps
# Should show argus-searxng running

docker compose -f /opt/searxng logs -f
# Check for errors

curl http://127.0.0.1:8888/
# Should return SearXNG home page (HTML)
```

If it's down:
```bash
docker compose -f /opt/searxng up -d
```

### Logs show "Bearer token invalid"

Token might be expired or mistyped. Regenerate (see **Rollback** section).

### Too many 401 errors - IP banned by fail2ban

Unban your IP:
```bash
fail2ban-client set argus unbanip <YOUR_IP>
```

Check current bans:
```bash
fail2ban-client status argus
```

## References

- **Design**: [docs/00-DESIGN.md](../docs/00-DESIGN.md) sec 9 (Deploy topology)
- **Roadmap**: [docs/02-ROADMAP.md](../docs/02-ROADMAP.md) P3 (Productionize the MCP)
- **Tool specs**: [docs/03-TOOL-SPECS.md](../docs/03-TOOL-SPECS.md)
- **Security audit**: [SECURITY-AUDIT.md](SECURITY-AUDIT.md)
- **SearXNG backend**: [searxng/README.md](searxng/README.md)
- **Hermes coexistence**: `../../08. Hermes AI Server/docs/ARSITEKTUR-HERMES-SUVA.md`

## Safe auto-update (poll main -> health-check -> auto-rollback)

When an approved change lands on `main` (PR-reviewed), the live server self-updates
within ~5 min. The model is **pull-only** (no inbound webhook port): a systemd timer
(`argus-update.timer`) polls `main` every 5 min and runs `argus-update.sh`, which:

1. **fast-forwards** `main` (ff-only - a force-pushed / divergent `main` is logged and
   skipped, never silently reset);
2. **skips the restart + health-gate for docs/benchmark-only commits** - a change that
   touches none of `src/`, `pyproject.toml`, `uv.lock`, `deploy/argus.service`, or
   `deploy/argus.env` does not affect the running service, so a README edit never
   triggers a prod restart;
3. reinstalls deps **only if the manifest changed**;
4. restarts `argus`, then **health-gates** by polling `/health` for ~30s;
5. **auto-rolls-back** to the prior commit (and reinstalls / restarts) if health does
   not come up.

It runs git as the `argus` user with `core.fileMode=false`, so executable-bit drift
(e.g. a `chmod +x` at install time) can never abort the ff-merge - this was a real
incident, now hardened. A no-change cycle is a silent no-op. The timer is independent
of `argus.service`; pausing it does not stop the server. Trust boundary is the GitHub
`main` branch, kept PR-gated.

This is **live** on the VPS.

<details><summary>One-time install (run as root, after the repo is at /opt/argus/app)</summary>

```bash
install -m 0755 /opt/argus/app/deploy/argus-update.sh /opt/argus/app/deploy/argus-update.sh
cp /opt/argus/app/deploy/argus-update.service /etc/systemd/system/argus-update.service
cp /opt/argus/app/deploy/argus-update.timer   /etc/systemd/system/argus-update.timer
systemctl daemon-reload
systemctl enable --now argus-update.timer
```

</details>

Operate it:

```bash
systemctl list-timers argus-update.timer        # next/last run
journalctl -u argus-update -n 50 --no-pager      # update + rollback log
systemctl start argus-update.service             # force an update check now
systemctl disable --now argus-update.timer       # pause auto-update
```

Notes:
- The script is **fast-forward only**. A non-ff `main` (force-push / divergence)
  is logged and skipped, never silently reset - fix manually then re-run.
- Trust boundary is the GitHub `main` branch; keep merges PR-gated.
- The timer is independent of `argus.service`; pausing it does not stop the server.

## Contact & Support

For issues:

1. Check logs: `journalctl -u argus -f`
2. Verify systemd status: `systemctl status argus`
3. Check nginx: `nginx -t && systemctl status nginx`
4. Verify SearXNG: `docker compose -f /opt/searxng ps`
5. Test fail2ban: `fail2ban-client status argus`

If stuck, contact the owner (Gifari) with:
- Full error log (journalctl + nginx error log)
- Output of `systemctl status argus`
- Result of `curl http://127.0.0.1:8090/health` (local)
- Result of `curl https://argus.gifariksuryo.xyz/health` (remote, with token)
