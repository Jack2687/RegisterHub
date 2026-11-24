#!/usr/bin/env bash
set -euo pipefail

cd "$(dirname "$0")"

PYTHON_BIN="${PYTHON_BIN:-python3}"
if ! command -v "$PYTHON_BIN" >/dev/null 2>&1; then
  PYTHON_BIN="python"
fi
if ! command -v "$PYTHON_BIN" >/dev/null 2>&1; then
  echo "[ERROR] Python 3.10+ not found in PATH." >&2
  exit 1
fi

if [[ ! -f "config.json" && -f "config.example.json" ]]; then
  cp "config.example.json" "config.json"
  echo "[INFO] Initialized config.json from config.example.json"
fi

PORT="${1:-18886}"
URL="http://127.0.0.1:${PORT}"
echo "[*] Starting RegisterHub Web Workbench at ${URL} ..."

if command -v open >/dev/null 2>&1; then
  (sleep 2 && open "$URL") >/dev/null 2>&1 &
elif command -v xdg-open >/dev/null 2>&1; then
  (sleep 2 && xdg-open "$URL") >/dev/null 2>&1 &
fi

exec "$PYTHON_BIN" main.py --web --port "$PORT"
