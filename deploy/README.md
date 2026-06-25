# Argus VPS Deployment Runbook

> **Status: DEPLOYED-LIVE.** Argus runs in production at
> `https://argus.gifariksuryo.xyz/mcp` on the SURIOTA VPS `103.172.172.29`
> (uvicorn `127.0.0.1:8090 --workers 1`, SearXNG docker `127.0.0.1:8888`,
> Let's Encrypt TLS, nginx, fail2ban). `/health` returns 200; `/mcp` returns 401
> without a bearer token. This runbook is the provisioning + operations reference;
> the steps below were executed for the live deploy and remain the re-provision
> recipe. The examples use `argus.gifariksuryo.xyz` (the live host).

## Contents

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

This directory contains the **configuration files** that provision Argus on the SURIOTA VPS (`103.172.172.29`, Ubuntu 24.04). Argus coexists with Hermes (:80) and SUVA (:8080) - it binds to `127.0.0.1:8090` locally, and nginx proxies the public HTTPS subdomain to it.

The P1+P2 exit gates passed and the service is live (see [Roadmap](../docs/02-ROADMAP.md)). Re-run the steps below to re-provision or to stand up a second instance; tests must be green locally first.

## Architecture

```
Claude Code CLI (HTTPS Bearer)
    |
    +-> nginx (argus.<domain>, TLS, fail2ban)
    |       |
    |       +-> /mcp           --> uvicorn 127.0.0.1:8090
    |       +-> /health        --> (monitoring, no auth)
    |       +-> /metrics       --> (Prometheus, optional IP allowlist)
    |
    +- SearXNG Docker
           +-> 127.0.0.1:8888 (JSON API, loopback only)
           +- Shared with Argus via httpx client

Systemd service:  argus.service (User=argus, EnvironmentFile=/etc/argus/argus.env)
Auth:             Bearer token (StaticTokenVerifier -> JWTVerifier)
TLS:              certbot (LetsEncrypt, auto-renewal)
Rate limit:       fail2ban (401 brute-force protection on /mcp)
```

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

Before running deployment, ensure:

1. **VPS Access**: SSH key-only to `103.172.172.29` as user `ai`
   ```bash
   ssh -i ~/.ssh/gifari_vps_ed25519 ai@103.172.172.29
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
scp -r -i ~/.ssh/gifari_vps_ed25519 deploy/ ai@103.172.172.29:/tmp/argus-deploy

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

**What it does:**

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

# Timeouts (seconds)
ARGUS_REQUEST_TIMEOUT=30
ARGUS_BROWSER_TIMEOUT=60

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

**Conflict Risk**: Hermes and Argus both want port 80/443. The provision script does NOT modify Hermes - **you must ensure your nginx upstream config multiplexes both via SNI or Host header routing**. See Hermes deployment guide for how to add Argus as a second upstream block.

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

This is **live** on the VPS. One-time install (run as root, after the repo is at
`/opt/argus/app`):

```bash
install -m 0755 /opt/argus/app/deploy/argus-update.sh /opt/argus/app/deploy/argus-update.sh
cp /opt/argus/app/deploy/argus-update.service /etc/systemd/system/argus-update.service
cp /opt/argus/app/deploy/argus-update.timer   /etc/systemd/system/argus-update.timer
systemctl daemon-reload
systemctl enable --now argus-update.timer
```

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
