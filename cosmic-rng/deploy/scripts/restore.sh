#!/usr/bin/env bash
# Cosmic RNG — restore the database from a backup file.
#   sudo bash deploy/scripts/restore.sh cosmic-rng-20260101-043000-scheduled.dump
set -euo pipefail

APP_DIR=${APP_DIR:-/opt/cosmic-rng}
APP_USER=${APP_USER:-cosmic}
FILE=${1:-}

die() { printf "\n\033[1;31mERROR: %s\033[0m\n" "$*" >&2; exit 1; }
[[ $EUID -eq 0 ]] || die "run as root (sudo)"
[[ -n "$FILE" ]] || die "usage: restore.sh <backup-filename>"

BACKUP_DIR=$(grep -E '^BACKUP_DIR=' "$APP_DIR/.env" | cut -d= -f2- || echo /var/backups/cosmic-rng)
PATH_TO="$BACKUP_DIR/$FILE"
[[ -f "$PATH_TO" ]] || die "not found: $PATH_TO"

cat <<WARN

  ⚠  This REPLACES the entire database with the contents of
        $PATH_TO
     All data created after that backup will be lost.

WARN
read -r -p "Type RESTORE to continue: " confirm
[[ "$confirm" == "RESTORE" ]] || die "aborted"

echo "Stopping the service…"
systemctl stop cosmic-rng

echo "Taking a safety backup of the current database first…"
sudo -u "$APP_USER" bash -c "cd '$APP_DIR/backend' && .venv/bin/python -m app.cli backup --kind manual" || true

echo "Restoring…"
sudo -u "$APP_USER" bash -c "cd '$APP_DIR/backend' && .venv/bin/python -m app.cli restore '$FILE' --yes"

echo "Starting the service…"
systemctl start cosmic-rng
sleep 3
curl -fsS http://127.0.0.1:8000/api/health && echo && echo "Restore complete ✓"
