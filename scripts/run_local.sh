#!/usr/bin/env bash
# Boot the full stack locally: MongoDB -> FastAPI :8000 -> UI :${UI_PORT:-3001}.
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"

MONGO_BIN="${MONGO_BIN:-$HOME/tools/mongodb-macos-aarch64-8.0.12/bin/mongod}"
DATA_DIR="${MONGO_DATA:-$HOME/tools/mongo-data}"
LOG_DIR="${TMPDIR:-/tmp}/recon-logs"
mkdir -p "$LOG_DIR"

if ! command -v mongod >/dev/null 2>&1; then
  if [ ! -x "$MONGO_BIN" ]; then
    echo "mongod not found. Install MongoDB or set MONGO_BIN." >&2
    exit 1
  fi
  mongod() { "$MONGO_BIN" "$@"; }
fi

if ! curl -s --max-time 2 localhost:27017 >/dev/null 2>&1; then
  mkdir -p "$DATA_DIR"
  mongod --dbpath "$DATA_DIR" --port 27017 --fork --logpath "$LOG_DIR/mongod.log"
  echo "MongoDB started (data: $DATA_DIR)"
else
  echo "MongoDB already running"
fi

if ! curl -s --max-time 2 localhost:8000/api/health >/dev/null 2>&1; then
  (cd backend && COOKIE_SECURE=false nohup ../.venv/bin/python -m uvicorn server:app \
      --port 8000 > "$LOG_DIR/backend.log" 2>&1 & disown)
  echo "Backend starting on :8000 (log: $LOG_DIR/backend.log)"
else
  echo "Backend already running"
fi

if [ ! -d frontend/build ]; then
  echo "Building frontend..."
  (cd frontend && npm run build)
fi

if ! curl -s -o /dev/null --max-time 2 localhost:${UI_PORT:-3001} >/dev/null 2>&1; then
  nohup node scripts/local_server.js "${UI_PORT:-3001}" frontend/build 8000 \
      > "$LOG_DIR/ui.log" 2>&1 & disown
  echo "UI starting on :3000"
else
  echo "UI already running on :${UI_PORT:-3001}"
fi

sleep 2
echo
curl -s localhost:8000/api/health && echo
echo
echo "  App   -> http://localhost:${UI_PORT:-3001}"
echo "  API   -> http://localhost:8000/api/health"
echo "  Logins: analyst@recon.io / analyst123 · controller@recon.io / controller123 · admin@recon.io / admin123"
