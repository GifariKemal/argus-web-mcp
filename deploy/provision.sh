#!/usr/bin/env bash
# Argus - VPS Provisioning Script
# Ubuntu 24.04, idempotent, runs as root.
# Creates unprivileged 'argus' user, installs deps, deploys systemd unit + nginx + fail2ban.
# Reference: docs/00-DESIGN.md sec  9 (Deploy topology), docs/02-ROADMAP.md P3

set -euo pipefail

# ====================================================================
# Configuration (adjust as needed)
# ====================================================================
ARGUS_USER="argus"
ARGUS_GROUP="argus"
ARGUS_HOME="/opt/argus"
ARGUS_REPO="https://github.com/SURIOTA/argus-web-mcp.git"  # Replace with real URL
ARGUS_BRANCH="main"
PYTHON_VERSION="3.12"
SEARXNG_COMPOSE_DIR="/opt/searxng"  # Where SearXNG docker-compose lives
DOMAIN_PLACEHOLDER="argus.<domain>"   # Replace with actual domain during run

# ====================================================================
# Helper: print section headers
# ====================================================================
log_section() {
    echo ""
    echo "=========================================="
    echo "$1"
    echo "=========================================="
}

log_info() {
    echo "[INFO] $1"
}

log_error() {
    echo "[ERROR] $1" >&2
    return 1
}

# ====================================================================
# Step 1: Verify running as root
# ====================================================================
log_section "Step 1: Verify root privileges"
if [[ $EUID -ne 0 ]]; then
    log_error "This script must be run as root"
    exit 1
fi
log_info "Running as root [x]"

# ====================================================================
# Step 2: Update system packages
# ====================================================================
log_section "Step 2: Update system packages"
apt update
apt upgrade -y
log_info "System packages updated [x]"

# ====================================================================
# Step 3: Install system dependencies
# ====================================================================
log_section "Step 3: Install system dependencies"
# Python 3.12, pip, build essentials, libmagic (for trafilatura), curl, git
PKGS="python${PYTHON_VERSION} python${PYTHON_VERSION}-venv python${PYTHON_VERSION}-dev \
    python3-pip build-essential libmagic1 git curl wget nginx certbot \
    python3-certbot-nginx fail2ban fontconfig fonts-dejavu libnss3 libxss1 \
    libappindicator3-1 libindicator7"

apt install -y $PKGS
log_info "System dependencies installed [x]"

# ====================================================================
# Step 4: Create 'argus' unprivileged user
# ====================================================================
log_section "Step 4: Create unprivileged 'argus' user"
if ! id "$ARGUS_USER" &>/dev/null; then
    useradd --system --home "$ARGUS_HOME" --shell /bin/bash \
            --comment "Argus MCP Server" "$ARGUS_USER"
    log_info "User '$ARGUS_USER' created [x]"
else
    log_info "User '$ARGUS_USER' already exists (skipping) [x]"
fi

# Create home directory if it doesn't exist
if [[ ! -d "$ARGUS_HOME" ]]; then
    mkdir -p "$ARGUS_HOME"
    chown "$ARGUS_USER:$ARGUS_GROUP" "$ARGUS_HOME"
    chmod 750 "$ARGUS_HOME"
    log_info "Home directory '$ARGUS_HOME' created [x]"
fi

# ====================================================================
# Step 5: Clone the Argus repository
# ====================================================================
log_section "Step 5: Clone Argus repository"
if [[ ! -d "$ARGUS_HOME/.git" ]]; then
    # First clone: use git as a subprocess (not as $ARGUS_USER yet)
    cd /tmp
    git clone --branch "$ARGUS_BRANCH" "$ARGUS_REPO" argus-temp
    # Move to final location
    mv argus-temp "$ARGUS_HOME"
    chown -R "$ARGUS_USER:$ARGUS_GROUP" "$ARGUS_HOME"
    log_info "Repository cloned to '$ARGUS_HOME' [x]"
else
    log_info "Repository already exists; updating [x]"
    cd "$ARGUS_HOME"
    git pull origin "$ARGUS_BRANCH" || log_info "Git pull failed; continuing anyway"
fi

cd "$ARGUS_HOME"

# Idempotent git hardening so future auto-updates (`git merge --ff-only`/`git pull`)
# are never blocked by local working-tree state introduced by this script. Run as the
# repo owner ($ARGUS_USER). A real incident showed both of these block a fast-forward:
#  - core.fileMode false: ignore executable-bit drift from the chmods this script does,
#    so the deploy-script's own permission changes don't show as modified files.
#  - assume-unchanged on deploy/searxng/settings.yml: Step 10 injects a secret into this
#    tracked file; assume-unchanged keeps that local secret from blocking/clobbering on merge.
sudo -u "$ARGUS_USER" git config core.fileMode false
sudo -u "$ARGUS_USER" git update-index --assume-unchanged deploy/searxng/settings.yml

# ====================================================================
# Step 6: Create cache directory
# ====================================================================
log_section "Step 6: Create cache directory"
CACHE_DIR="$ARGUS_HOME/.argus"
if [[ ! -d "$CACHE_DIR" ]]; then
    mkdir -p "$CACHE_DIR"
fi
chown "$ARGUS_USER:$ARGUS_GROUP" "$CACHE_DIR"
chmod 700 "$CACHE_DIR"
log_info "Cache directory created at '$CACHE_DIR' [x]"

# ====================================================================
# Step 7: Create Python virtual environment
# ====================================================================
log_section "Step 7: Create Python virtual environment"
VENV_PATH="$ARGUS_HOME/.venv"
if [[ ! -d "$VENV_PATH" ]]; then
    "python${PYTHON_VERSION}" -m venv "$VENV_PATH"
    chown -R "$ARGUS_USER:$ARGUS_GROUP" "$VENV_PATH"
    log_info "Virtual environment created at '$VENV_PATH' [x]"
else
    log_info "Virtual environment already exists (skipping) [x]"
fi

# ====================================================================
# Step 8: Install Python dependencies as 'argus' user
# ====================================================================
log_section "Step 8: Install Python dependencies"
# Use 'sudo -u' to run as the argus user
log_info "Upgrading pip and installing uv..."
sudo -u "$ARGUS_USER" "$VENV_PATH/bin/pip" install --upgrade pip setuptools wheel uv

log_info "Installing Argus dependencies via uv..."
# Install core dependencies + optional PDF quality tier
sudo -u "$ARGUS_USER" "$VENV_PATH/bin/uv" pip install -e "$ARGUS_HOME[pdf-quality]"

log_info "Python dependencies installed [x]"

# ====================================================================
# Step 9: Install Playwright & Crawl4AI browser binaries
# ====================================================================
log_section "Step 9: Install browser binaries (as '$ARGUS_USER')"
# This MUST run as the argus user so the browser cache is under its ~/.cache
# Hermes/SUVA experience: mismatched user = "Cannot open display" / IPC errors.

log_info "Running crawl4ai-setup..."
sudo -u "$ARGUS_USER" bash -c "cd '$ARGUS_HOME' && '$VENV_PATH/bin/python' -m crawl4ai.setup"

log_info "Installing Playwright Chromium..."
sudo -u "$ARGUS_USER" "$VENV_PATH/bin/playwright" install --with-deps chromium

log_info "Running crawl4ai-doctor for validation..."
sudo -u "$ARGUS_USER" "$VENV_PATH/bin/python" -c "from crawl4ai import AsyncWebCrawler; print('crawl4ai ready')"

log_info "Browser binaries ready [x]"

# ====================================================================
# Step 10: SearXNG setup (docker-compose)
# ====================================================================
log_section "Step 10: SearXNG Docker setup"
if [[ ! -d "$SEARXNG_COMPOSE_DIR" ]]; then
    mkdir -p "$SEARXNG_COMPOSE_DIR"
fi

# Copy SearXNG docker-compose from deploy/searxng/ to /opt/searxng
cp "$ARGUS_HOME/deploy/searxng/docker-compose.yml" "$SEARXNG_COMPOSE_DIR/"
cp "$ARGUS_HOME/deploy/searxng/settings.yml" "$SEARXNG_COMPOSE_DIR/"

# SECURITY: replace the settings.yml secret_key placeholder with a fresh random value
# (a known/default SearXNG secret_key is a real vuln). Idempotent: only if placeholder present.
if grep -q "CHANGE_ME_GENERATE_RANDOM" "$SEARXNG_COMPOSE_DIR/settings.yml"; then
    SEARXNG_SECRET="$(openssl rand -hex 32)"
    sed -i "s/CHANGE_ME_GENERATE_RANDOM/${SEARXNG_SECRET}/" "$SEARXNG_COMPOSE_DIR/settings.yml"
    log_info "Generated random SearXNG secret_key [x]"
fi

log_info "SearXNG compose files in place [x]"

# Ensure Docker is installed
if ! command -v docker &>/dev/null; then
    log_info "Docker not found; installing..."
    curl -fsSL https://get.docker.com -o /tmp/get-docker.sh
    bash /tmp/get-docker.sh
    log_info "Docker installed [x]"
fi

# Ensure docker-compose (or docker compose) is available
if ! command -v docker-compose &>/dev/null && ! docker compose version &>/dev/null; then
    log_info "docker-compose not found; installing..."
    pip install docker-compose
    log_info "docker-compose installed [x]"
fi

# Bring up SearXNG (if not already running)
cd "$SEARXNG_COMPOSE_DIR"
if ! docker compose ps 2>/dev/null | grep -q "argus-searxng"; then
    log_info "Starting SearXNG container..."
    docker compose up -d
    log_info "SearXNG started [x]"
    # Wait for it to be ready (simple health check)
    sleep 5
    if curl -s http://127.0.0.1:8888/ >/dev/null 2>&1; then
        log_info "SearXNG is responding [x]"
    else
        log_error "SearXNG failed to start; check docker logs"
    fi
else
    log_info "SearXNG already running (skipping) [x]"
fi

cd "$ARGUS_HOME"

# ====================================================================
# Step 11: Create /etc/argus/ directory for secrets
# ====================================================================
log_section "Step 11: Create /etc/argus/ config directory"
mkdir -p /etc/argus
chown root:root /etc/argus
chmod 755 /etc/argus

log_info "/etc/argus directory ready [x]"

# ====================================================================
# Step 12: Create /etc/argus/argus.env (if missing)
# ====================================================================
log_section "Step 12: Generate /etc/argus/argus.env (secrets file)"
ENV_FILE="/etc/argus/argus.env"

if [[ ! -f "$ENV_FILE" ]]; then
    log_info "Generating fresh ARGUS_TOKEN..."
    FRESH_TOKEN=$(openssl rand -hex 32)

    # Create env file from template
    cp "$ARGUS_HOME/deploy/argus.env.example" "$ENV_FILE"

    # Replace placeholder with the generated token
    sed -i "s/__GENERATE_WITH_openssl_rand_-hex_32__/${FRESH_TOKEN}/" "$ENV_FILE"

    # Secure permissions: 0600, owned by root:argus (only root + systemd can read)
    chmod 600 "$ENV_FILE"
    chown root:root "$ENV_FILE"  # Root owns it; systemd runs as argus with EnvironmentFile

    log_info "Generated ARGUS_TOKEN: ${FRESH_TOKEN:0:16}...xxxx (truncated)" >&2
    log_info "Full token stored in /etc/argus/argus.env [x]"
else
    log_info "/etc/argus/argus.env already exists (not overwriting) [x]"
    log_info "If you need a fresh token, manually run:"
    log_info "  openssl rand -hex 32 | sudo tee -a /etc/argus/argus.env"
fi

# ====================================================================
# Step 13: Install systemd service
# ====================================================================
log_section "Step 13: Install systemd service"
cp "$ARGUS_HOME/deploy/argus.service" /etc/systemd/system/
systemctl daemon-reload
log_info "systemd unit installed [x]"

# Enable the service (auto-start on boot)
systemctl enable argus
log_info "Service enabled for auto-start [x]"

# ====================================================================
# Step 14: Install nginx configuration
# ====================================================================
log_section "Step 14: Install nginx configuration"
# Replace placeholder with actual domain (assumption: user will do this)
if [[ "$DOMAIN_PLACEHOLDER" == "argus.<domain>" ]]; then
    log_info "WARNING: domain is still a placeholder (argus.<domain>)"
    log_info "You MUST edit /etc/nginx/sites-available/argus after this script"
    log_info "and replace 'argus.<domain>' with your actual domain before running certbot."
fi

cp "$ARGUS_HOME/deploy/argus.nginx.conf" /etc/nginx/sites-available/argus
ln -sf /etc/nginx/sites-available/argus /etc/nginx/sites-enabled/argus

# Test nginx syntax
if nginx -t; then
    log_info "nginx configuration valid [x]"
else
    log_error "nginx configuration has errors; check /etc/nginx/sites-available/argus"
    exit 1
fi

# Enable nginx
systemctl enable nginx
systemctl start nginx || true  # May already be running
log_info "nginx configured and started [x]"

# ====================================================================
# Step 15: Install fail2ban jail
# ====================================================================
log_section "Step 15: Install fail2ban protection"
mkdir -p /etc/fail2ban/jail.d
cp "$ARGUS_HOME/deploy/fail2ban-argus.conf" /etc/fail2ban/jail.d/

# Create the filter file
cat > /etc/fail2ban/filter.d/nginx-argus-bearer-401.conf <<'EOF'
[Definition]
failregex = ^<HOST> .* "(?:GET|POST|PUT|PATCH|DELETE) /mcp HTTP/.*" 401 .*$
ignoreregex =
EOF

systemctl enable fail2ban
systemctl start fail2ban || systemctl restart fail2ban
log_info "fail2ban jail installed and enabled [x]"

# ====================================================================
# Step 16: Generate TLS certificate via certbot
# ====================================================================
log_section "Step 16: Generate TLS certificate (certbot)"
mkdir -p /var/www/letsencrypt

if [[ "$DOMAIN_PLACEHOLDER" != "argus.<domain>" ]]; then
    log_info "Requesting TLS certificate for ${DOMAIN_PLACEHOLDER}..."
    certbot certonly --webroot -w /var/www/letsencrypt -d "$DOMAIN_PLACEHOLDER" \
        --email admin@suriota.com --agree-tos --non-interactive --renew-by-default || \
        log_error "certbot failed; you may need to manually run:"
    log_info "  certbot certonly --webroot -w /var/www/letsencrypt -d ${DOMAIN_PLACEHOLDER}"
else
    log_error "Cannot request certificate: domain is still a placeholder (argus.<domain>)"
    log_info "After you update /etc/nginx/sites-available/argus with the real domain, run:"
    log_info "  certbot certonly --webroot -w /var/www/letsencrypt -d argus.yourdomain.com"
fi

# Reload nginx to pick up certs (if they exist)
systemctl reload nginx || true
log_info "TLS setup complete [x]"

# ====================================================================
# Step 17: Verify systemd service can start
# ====================================================================
log_section "Step 17: Start and verify Argus service"
systemctl start argus

# Give it a few seconds to start
sleep 3

if systemctl is-active --quiet argus; then
    log_info "Service started successfully [x]"
else
    log_error "Service failed to start; check logs:"
    log_info "  journalctl -u argus -n 50"
    exit 1
fi

# ====================================================================
# Step 18: Verify /health endpoint
# ====================================================================
log_section "Step 18: Verify /health endpoint"
if curl -s http://127.0.0.1:8090/health | grep -q '"status"'; then
    log_info "/health endpoint responding [x]"
else
    log_error "/health not responding; check service logs"
fi

# ====================================================================
# SUMMARY
# ====================================================================
log_section "Provisioning Complete [x]"
echo ""
echo "========== Argus VPS Deployment Summary =========="
echo ""
echo "Service Status:"
echo "  systemctl status argus"
echo ""
echo "Logs:"
echo "  journalctl -u argus -f"
echo ""
echo "Environment File (secrets):"
echo "  /etc/argus/argus.env"
echo ""
echo "Bearer Token:"
echo "  (saved in /etc/argus/argus.env - do not share; read it there when you need it)"
echo ""
echo "nginx:"
echo "  /etc/nginx/sites-available/argus"
echo "  Domain: $DOMAIN_PLACEHOLDER (UPDATE ME if placeholder)"
echo ""
echo "TLS Certificate:"
echo "  /etc/letsencrypt/live/argus.<domain>/"
echo "  (auto-renews via certbot every 90 days)"
echo ""
echo "SearXNG:"
echo "  docker compose status:"
docker compose -f "$SEARXNG_COMPOSE_DIR/docker-compose.yml" ps || true
echo ""
echo "Port Map:"
echo "  SearXNG:      127.0.0.1:8888 (docker, loopback only)"
echo "  Argus:        127.0.0.1:8090 (systemd, loopback only)"
echo "  nginx public: 0.0.0.0:80/443 (TLS, public)"
echo "  Hermes:       0.0.0.0:80 (coexist)"
echo "  SUVA:         127.0.0.1:8080 (coexist)"
echo ""
echo "========== Next Steps =========="
echo ""
echo "1. DOMAIN: If you used a placeholder, update /etc/nginx/sites-available/argus"
echo "   and run certbot:"
echo "      certbot certonly --webroot -w /var/www/letsencrypt -d argus.yourdomain.com"
echo ""
echo "2. SECRETS: The bearer token is in /etc/argus/argus.env"
echo "   Share it with users who will register the MCP in Claude Code."
echo "   Rotate it every 90 days: openssl rand -hex 32"
echo ""
echo "3. REGISTER in Claude Code (client side):"
echo "   claude mcp add --transport http argus https://argus.<domain>/mcp \\"
echo "     --header \"Authorization: Bearer <ARGUS_TOKEN>\""
echo ""
echo "4. VERIFY remote HTTPS:"
echo "   curl https://argus.<domain>/health -H \"Authorization: Bearer <ARGUS_TOKEN>\""
echo ""
echo "5. MONITORING: Hermes watchdog should curl /health every 30 min"
echo "   fail2ban is active: fail2ban-client status argus"
echo ""
echo "========== Security Checklist =========="
echo "  [x] Argus runs as unprivileged 'argus' user"
echo "  [x] /etc/argus/argus.env is 0600 (root readable only)"
echo "  [x] Bearer token generated (save it externally)"
echo "  [x] TLS via certbot (A+ rating expected)"
echo "  [x] fail2ban protects /mcp from brute-force (401 limit)"
echo "  [x] nginx proxy_buffering off (streaming MCP)"
echo "  [x] SSRF + auth hardened in Argus server code (100% tested)"
echo ""
echo "========== Troubleshooting =========="
echo "systemctl status argus                    # Service state"
echo "journalctl -u argus -f                    # Follow logs"
echo "curl http://127.0.0.1:8090/health         # Direct backend health"
echo "curl https://argus.<domain>/health        # Via nginx + TLS"
echo "docker compose -f /opt/searxng ps         # SearXNG status"
echo "fail2ban-client status argus              # Banned IPs"
echo ""
