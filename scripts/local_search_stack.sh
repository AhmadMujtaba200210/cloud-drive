#!/usr/bin/env bash
# Free, self-hosted search stack for PartsBridge PK — no Docker, no paid APIs.
#   SearXNG          (metasearch, JSON API)        -> http://127.0.0.1:8888
#   Firecrawl Local  (page scraping to markdown)   -> http://127.0.0.1:3002
# Usage: scripts/local_search_stack.sh install | start | stop | status
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
SX_DIR="${SEARXNG_DIR:-$HOME/.local/share/partsbridge/searxng}"
RUN_DIR="${RUN_DIR:-$HOME/.local/state/partsbridge}"
mkdir -p "$RUN_DIR"

install() {
  command -v cargo >/dev/null || { echo "Install Rust first: https://rustup.rs"; exit 1; }
  command -v firecrawl-local >/dev/null || cargo install firecrawl-local
  if [ ! -d "$SX_DIR/src" ]; then
    mkdir -p "$SX_DIR"
    git clone --depth 1 https://github.com/searxng/searxng.git "$SX_DIR/src"   # official source; the PyPI name is not official
  fi
  [ -d "$SX_DIR/venv" ] || python3 -m venv "$SX_DIR/venv"
  "$SX_DIR/venv/bin/pip" install -q -U pip setuptools wheel
  "$SX_DIR/venv/bin/pip" install -q -r "$SX_DIR/src/requirements.txt"
  echo "installed. Next: $0 start"
}

start() {
  if ! curl -sf http://127.0.0.1:8888/healthz >/dev/null 2>&1; then
    ( cd "$SX_DIR/src" && SEARXNG_SETTINGS_PATH="$ROOT/infra/searxng/settings.yml" \
      SEARXNG_SECRET="$(python3 -c 'import secrets;print(secrets.token_hex(32))')" \
      nohup "$SX_DIR/venv/bin/python" -m searx.webapp >"$RUN_DIR/searxng.log" 2>&1 & echo $! >"$RUN_DIR/searxng.pid" )
  fi
  if ! curl -s -o /dev/null http://127.0.0.1:3002/ 2>/dev/null; then
    # FIRECRAWL_CLOUD=off: never send anything to Firecrawl Cloud; search is done by SearXNG instead.
    ( FIRECRAWL_CLOUD=off nohup firecrawl-local serve >"$RUN_DIR/firecrawl.log" 2>&1 & echo $! >"$RUN_DIR/firecrawl.pid" )
  fi
  for _ in $(seq 1 30); do
    curl -sf http://127.0.0.1:8888/healthz >/dev/null 2>&1 && curl -s -o /dev/null http://127.0.0.1:3002/ && break; sleep 1
  done
  status
}

stop() {
  for s in searxng firecrawl; do [ -f "$RUN_DIR/$s.pid" ] && kill "$(cat "$RUN_DIR/$s.pid")" 2>/dev/null || true; rm -f "$RUN_DIR/$s.pid"; done
  echo stopped
}

status() {
  curl -sf http://127.0.0.1:8888/healthz >/dev/null 2>&1 && echo "SearXNG:         up   (SEARXNG_URL=http://127.0.0.1:8888)" || echo "SearXNG:         DOWN (log $RUN_DIR/searxng.log)"
  curl -s -o /dev/null http://127.0.0.1:3002/ 2>/dev/null && echo "Firecrawl Local: up   (FIRECRAWL_BASE_URL=http://127.0.0.1:3002)" || echo "Firecrawl Local: DOWN (log $RUN_DIR/firecrawl.log) - pages fall back to plain HTTP fetch"
}

"${1:-status}"
