#!/usr/bin/env bash
# Local development: starts PostgreSQL, the API (port 8000) and Vite (port 5173).
set -euo pipefail
cd "$(dirname "$0")/../.."
ROOT=$(pwd)

command -v pg_isready >/dev/null || { echo "PostgreSQL client tools are required"; exit 1; }
pg_isready -q -h 127.0.0.1 || { echo "Starting PostgreSQL…"; sudo service postgresql start || sudo systemctl start postgresql; }

if [[ ! -f backend/.env && ! -f .env ]]; then
  echo "Creating backend/.env for development"
  cat > backend/.env <<'ENV'
ENVIRONMENT=development
DATABASE_URL=postgresql+asyncpg://cosmic:cosmic@127.0.0.1:5432/cosmic_rng
COOKIE_SECURE=false
PUBLIC_BASE_URL=http://localhost:5173
ALLOWED_ORIGINS=http://localhost:5173,http://127.0.0.1:5173,http://localhost:8000
DEV_LOGIN_ENABLED=true
ADMIN_DISCORD_IDS=1324938326741876758
EVENT_BUS=postgres
ENV
fi

cd "$ROOT/backend"
[[ -d .venv ]] || python3 -m venv .venv
. .venv/bin/activate
pip install -q -r requirements-dev.txt
python -m app.cli migrate
python -m app.cli seed
echo "API   → http://127.0.0.1:8000"
uvicorn app.main:app --host 127.0.0.1 --port 8000 --reload &
API_PID=$!
trap 'kill $API_PID 2>/dev/null || true' EXIT

cd "$ROOT/frontend"
[[ -d node_modules ]] || npm install --no-audit --no-fund
echo "Game  → http://localhost:5173"
npm run dev
