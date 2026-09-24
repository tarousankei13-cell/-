#!/usr/bin/env bash
# Cosmic RNG — first-time server setup (Ubuntu/Debian).
# Run as root:  sudo bash deploy/scripts/setup.sh
set -euo pipefail

APP_DIR=${APP_DIR:-/opt/cosmic-rng}
APP_USER=${APP_USER:-cosmic}
DB_NAME=${DB_NAME:-cosmic_rng}
DB_USER=${DB_USER:-cosmic}

log() { printf "\n\033[1;36m==> %s\033[0m\n" "$*"; }
die() { printf "\n\033[1;31mERROR: %s\033[0m\n" "$*" >&2; exit 1; }
[[ $EUID -eq 0 ]] || die "run as root (sudo)"

log "Installing system packages"
export DEBIAN_FRONTEND=noninteractive
apt-get update -qq
apt-get install -y --no-install-recommends \
  python3 python3-venv python3-dev build-essential \
  postgresql postgresql-contrib \
  nginx certbot python3-certbot-nginx \
  curl ca-certificates gnupg git

if ! command -v node >/dev/null || [[ $(node -v | sed 's/v\([0-9]*\).*/\1/') -lt 20 ]]; then
  log "Installing Node.js 22 (for the frontend build)"
  curl -fsSL https://deb.nodesource.com/setup_22.x | bash -
  apt-get install -y nodejs
fi

log "Creating service user: $APP_USER"
id -u "$APP_USER" &>/dev/null || useradd --system --create-home --shell /usr/sbin/nologin "$APP_USER"

log "Creating directories"
mkdir -p "$APP_DIR" /var/log/cosmic-rng /var/backups/cosmic-rng
chown -R "$APP_USER:$APP_USER" /var/log/cosmic-rng /var/backups/cosmic-rng
chmod 750 /var/backups/cosmic-rng

log "Configuring PostgreSQL"
systemctl enable --now postgresql
DB_PASS=$(python3 -c "import secrets;print(secrets.token_urlsafe(24))")
if sudo -u postgres psql -tAc "SELECT 1 FROM pg_roles WHERE rolname='$DB_USER'" | grep -q 1; then
  echo "    role $DB_USER already exists — leaving its password unchanged"
  DB_PASS=""
else
  sudo -u postgres psql -qc "CREATE ROLE $DB_USER LOGIN PASSWORD '$DB_PASS';"
fi
sudo -u postgres psql -tAc "SELECT 1 FROM pg_database WHERE datname='$DB_NAME'" | grep -q 1 \
  || sudo -u postgres createdb -O "$DB_USER" "$DB_NAME"

if [[ ! -f "$APP_DIR/.env" ]]; then
  log "Creating $APP_DIR/.env"
  cp "$(dirname "$0")/../../.env.example" "$APP_DIR/.env"
  SECRET=$(python3 -c "import secrets;print(secrets.token_urlsafe(48))")
  sed -i "s|^SECRET_KEY=.*|SECRET_KEY=$SECRET|" "$APP_DIR/.env"
  [[ -n "$DB_PASS" ]] && sed -i "s|^DATABASE_URL=.*|DATABASE_URL=postgresql+asyncpg://$DB_USER:$DB_PASS@127.0.0.1:5432/$DB_NAME|" "$APP_DIR/.env"
  chown "$APP_USER:$APP_USER" "$APP_DIR/.env"
  chmod 600 "$APP_DIR/.env"
  echo
  echo "  ⚠ Now edit $APP_DIR/.env and set:"
  echo "      PUBLIC_BASE_URL, DISCORD_CLIENT_ID, DISCORD_CLIENT_SECRET,"
  echo "      DISCORD_REDIRECT_URI, ADMIN_DISCORD_IDS"
else
  echo "    $APP_DIR/.env already exists — left untouched"
fi

log "Installing systemd units"
SRC=$(cd "$(dirname "$0")/.." && pwd)
cp "$SRC/systemd/cosmic-rng.service" "$SRC/systemd/cosmic-backup.service" "$SRC/systemd/cosmic-backup.timer" /etc/systemd/system/
systemctl daemon-reload
systemctl enable cosmic-backup.timer

cat <<NEXT

────────────────────────────────────────────────────────────────────────
Setup complete. Next steps:

  1. Put the code in $APP_DIR (git clone, or rsync this checkout).
  2. Edit $APP_DIR/.env   (Discord OAuth + your domain + admin IDs)
  3. sudo bash $APP_DIR/deploy/scripts/deploy.sh
  4. Nginx + TLS:
       sudo cp $APP_DIR/deploy/nginx/cosmic-rng.conf /etc/nginx/sites-available/cosmic-rng
       sudo sed -i 's/rng.example.com/YOUR.DOMAIN/g' /etc/nginx/sites-available/cosmic-rng
       sudo ln -sf /etc/nginx/sites-available/cosmic-rng /etc/nginx/sites-enabled/
       sudo certbot --nginx -d YOUR.DOMAIN
       sudo nginx -t && sudo systemctl reload nginx
────────────────────────────────────────────────────────────────────────
NEXT
