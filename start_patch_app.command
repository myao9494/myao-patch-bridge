#!/bin/sh
cd "$(dirname "$0")" || exit 1
if [ ! -x ".venv/bin/python" ]; then
  echo "Run: uv sync --extra dev"
  exit 1
fi
PORT=17345
PIDS="$(lsof -tiTCP:"$PORT" -sTCP:LISTEN 2>/dev/null || true)"
if [ -n "$PIDS" ]; then
  echo "Stopping the existing process on port $PORT..."
  kill $PIDS 2>/dev/null || true
  for _ in $(seq 1 20); do
    if ! lsof -tiTCP:"$PORT" -sTCP:LISTEN >/dev/null 2>&1; then
      break
    fi
    sleep 0.1
  done
  PIDS="$(lsof -tiTCP:"$PORT" -sTCP:LISTEN 2>/dev/null || true)"
  if [ -n "$PIDS" ]; then kill -9 $PIDS 2>/dev/null || true; fi
fi

PYTHONPATH="$(pwd)/src" exec .venv/bin/python -m rep_patch
