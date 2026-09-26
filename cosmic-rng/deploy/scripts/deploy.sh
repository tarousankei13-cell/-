#!/usr/bin/env bash
# Cosmic RNG — build and (re)start the application. Safe to run repeatedly.
#   sudo bash deploy/scripts/deploy.sh
set -euo pipefail

APP_DIR=${APP_DIR:-/opt/cosmic-rng}
APP_USER=${APP_USER:-cosmic}

log() { printf "\n\033[1;36m==> %s\033[0m\n" "$*"; }
die() { printf "\n\033[1;31mERROR: %s\033[0m\n" "$*" >&2; exit 1; }
[[ $EUID -eq 0 ]] || die "run as root (sudo)"
[[ -f "$APP_DIR/.env" ]] || die "$APP_DIR/.env is missing — run setup.sh first"

cd "$APP_DIR"
chown -R "$APP_USER:$APP_USER" "$APP_DIR"

log "Python dependencies"
sudo -u "$APP_USER" bash -c "
  cd '$APP_DIR/backend'
  [[ -d .venv ]] || python3 -m venv .venv
  .venv/bin/pip install --quiet --upgrade pip
  .venv/bin/pip install --quiet -r requirements.txt
"

log "Database migration"
sudo -u "$APP_USER" bash -c "cd '$APP_DIR/backend' && .venv/bin/python -m app.cli migrate"

log "Content seed (inserts only what is missing)"
sudo -u "$APP_USER" bash -c "cd '$APP_DIR/backend' && .venv/bin/python -m app.cli seed"

log "Frontend build"
sudo -u "$APP_USER" bash -c "cd '$APP_DIR/frontend' && npm ci --no-audit --no-fund && npm run build"

log "Restarting service"
systemctl restart cosmic-rng
sleep 3
systemctl is-active --quiet cosmic-rng || { journalctl -u cosmic-rng -n 40 --no-pager; die "service failed to start"; }

log "Health check"
for i in $(seq 1 20); do
  if curl -fsS http://127.0.0.1:8000/api/health >/dev/null; then
    curl -sS http://127.0.0.1:8000/api/health; echo
    log "Deploy complete ✓"
    exit 0
  fi
  sleep 1
done
journalctl -u cosmic-rng -n 40 --no-pager
die "health check failed"
