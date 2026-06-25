#!/usr/bin/env bash
# Argus safe auto-update.
#
# Polls the tracked branch (main), fast-forwards, reinstalls deps only if the
# manifest changed, restarts the service, then HEALTH-GATES the result and
# AUTO-ROLLS-BACK to the prior commit if /health does not come up. No inbound
# webhook port (pull model, driven by argus-update.timer). Idempotent and a
# silent no-op when already up to date.
#
# Runs as root (systemd oneshot): root can `systemctl restart` and drop to the
# argus user for git/pip. Trust boundary = the GitHub main branch, which is
# review-gated by PR before merge.
set -uo pipefail

APP=/opt/argus/app
SVC_USER=argus
SERVICE=argus
HEALTH=http://127.0.0.1:8090/health
BRANCH=main
TAG=argus-update

log() { logger -t "$TAG" -- "$*" 2>/dev/null || true; echo "[$TAG] $*"; }

cd "$APP" 2>/dev/null || { log "FATAL: app dir $APP missing"; exit 1; }

# git as the repo-owning user (repo is owned by argus:argus under ProtectSystem).
# core.fileMode=false: ignore executable-bit drift so a chmod'd deploy script (or
# any mode-only change in the working tree) can NEVER block the ff-merge. This was
# a real incident: `chmod +x argus-update.sh` at install left a mode diff that
# aborted the merge the first time a commit touched that file.
g() { sudo -u "$SVC_USER" git -C "$APP" -c core.fileMode=false "$@"; }

g fetch --quiet origin "$BRANCH" || { log "fetch failed (network?); skip this cycle"; exit 0; }
local_rev=$(g rev-parse HEAD)
remote_rev=$(g rev-parse "origin/$BRANCH")
[ "$local_rev" = "$remote_rev" ] && exit 0   # up to date: the common case, stay quiet

log "update: ${local_rev:0:8} -> ${remote_rev:0:8} (ff-only $BRANCH)"
# Fast-forward only: never silently discard local divergence (manual fix instead).
if ! g merge --ff-only "origin/$BRANCH"; then
  log "ERROR: $BRANCH not fast-forwardable; manual intervention needed"; exit 1
fi

# Docs/benchmark-only commits don't change the running service, so skip the
# restart + health-gate for them (a prod restart on a README edit is pure risk).
# A path is "runtime" if it touches code/deps/service config; everything else
# (docs/, benchmark/, *.md, ...) is non-runtime. grep -q -> match=restart.
changed=$(g diff --name-only "$local_rev" "$remote_rev")
if ! grep -Eq '^(src/|pyproject\.toml$|uv\.lock$|deploy/argus\.service$|deploy/argus\.env)' <<<"$changed"; then
  log "no code change - skipping restart (docs/benchmark-only): ${local_rev:0:8} -> ${remote_rev:0:8}"
  exit 0
fi

# Reinstall deps only when the manifest actually changed (avoids a slow no-op).
# ponytail: uses the venv's pip; if the venv was built by uv without pip this
# logs a warning rather than failing - dep-changing updates are rare and the
# health gate below still catches a broken install (-> rollback).
reinstall() {
  g diff --quiet "$1" "$2" -- pyproject.toml uv.lock && return 0
  log "dependency manifest changed; reinstalling"
  sudo -u "$SVC_USER" "$APP/.venv/bin/python" -m pip install -e "$APP" >/dev/null 2>&1 \
    || log "WARN: pip install returned nonzero (check venv/uv)"
}
reinstall "$local_rev" "$remote_rev"

systemctl restart "$SERVICE"

# Health gate: poll /health for ~30s before declaring success.
ok=0
for _ in $(seq 1 15); do
  sleep 2
  if curl -fsS -m 4 "$HEALTH" >/dev/null 2>&1; then ok=1; break; fi
done

if [ "$ok" = 1 ]; then
  log "OK: now at ${remote_rev:0:8} and healthy"
  exit 0
fi

# Auto-rollback: restore the prior commit, reinstall if deps moved, restart.
log "ERROR: unhealthy after update; rolling back to ${local_rev:0:8}"
g reset --hard "$local_rev"
reinstall "$remote_rev" "$local_rev"
systemctl restart "$SERVICE"
log "rolled back to ${local_rev:0:8} (update ${remote_rev:0:8} left unhealthy - investigate)"
exit 1
