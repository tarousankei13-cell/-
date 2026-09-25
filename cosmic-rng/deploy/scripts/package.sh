#!/usr/bin/env bash
# Build the distributable ZIP: the whole project with a prebuilt frontend, so it
# starts on a machine that has Python and nothing else.
#
#   deploy/scripts/package.sh 1.4      ->  cosmic-rng-v1.4.zip in the repo root
set -euo pipefail

VERSION="${1:?usage: package.sh <version>}"
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
OUT="$ROOT/cosmic-rng-v${VERSION}.zip"
STAGE="$(mktemp -d)"
trap 'rm -rf "$STAGE"' EXIT
DEST="$STAGE/cosmic-rng"

echo "==> building the frontend"
(cd "$ROOT/frontend" && npm run build >/dev/null)
[ -f "$ROOT/frontend/dist/index.html" ] || { echo "frontend build produced no dist/index.html"; exit 1; }

echo "==> staging"
mkdir -p "$DEST"
cp "$ROOT/main.py" "$ROOT/README.md" "$ROOT/SETUP.md" "$DEST/"
cp "$ROOT/backend/requirements.txt" "$DEST/requirements.txt"
[ -f "$ROOT/.env.example" ] && cp "$ROOT/.env.example" "$DEST/"
# -a, not -r: -r follows symlinks, so a link inside node_modules (a linked dev
# tool, a pnpm store) would be copied in full before the prune below runs.
cp -a "$ROOT/backend" "$ROOT/frontend" "$ROOT/deploy" "$DEST/"

# Nothing machine-local travels: caches, virtualenvs, node_modules, a developer's
# database, or the previous ZIPs.
find "$DEST" \( -name __pycache__ -o -name .pytest_cache -o -name .mypy_cache -o -name node_modules \
     -o -name .venv -o -name .DS_Store -o -name '*.pyc' \) -prune -exec rm -rf {} + 2>/dev/null || true
rm -rf "$DEST/backend/data" "$DEST/data" "$DEST/backups" "$DEST/frontend/.vite"

echo "==> zipping"
rm -f "$OUT"
(cd "$STAGE" && zip -qr "$OUT" cosmic-rng)
echo "==> $OUT  ($(du -h "$OUT" | cut -f1), $(unzip -l "$OUT" | tail -1 | awk '{print $2}') files)"
