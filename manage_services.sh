#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
RUN_DIR="$ROOT_DIR/.run"
LOG_DIR="$ROOT_DIR/.logs"
VENV_ACTIVATE="$ROOT_DIR/.venv/bin/activate"
ENV_FILE="$ROOT_DIR/.env"

MCP_PID_FILE="$RUN_DIR/mcp_server.pid"
WEB_PID_FILE="$RUN_DIR/webapi.pid"
MCP_LOG_FILE="$LOG_DIR/mcp_server.log"
WEB_LOG_FILE="$LOG_DIR/webapi.log"

MCP_TRANSPORT="${MCP_TRANSPORT:-streamable-http}"
MCP_HOST="${MCP_HOST:-127.0.0.1}"
MCP_PORT="${MCP_PORT:-8000}"
WEBAPI_HOST="${WEBAPI_HOST:-0.0.0.0}"
WEBAPI_PORT="${WEBAPI_PORT:-8080}"

mkdir -p "$RUN_DIR" "$LOG_DIR"

load_env_file() {
  if [[ -f "$ENV_FILE" ]]; then
    # Export variables loaded from .env for this script process.
    set -a
    # shellcheck disable=SC1090
    source "$ENV_FILE"
    set +a
  fi
}

load_env_file

# Re-evaluate defaults after loading .env so file values are honored.
MCP_TRANSPORT="${MCP_TRANSPORT:-streamable-http}"
MCP_HOST="${MCP_HOST:-127.0.0.1}"
MCP_PORT="${MCP_PORT:-8000}"
WEBAPI_HOST="${WEBAPI_HOST:-0.0.0.0}"
WEBAPI_PORT="${WEBAPI_PORT:-8080}"

require_venv() {
  if [[ ! -f "$VENV_ACTIVATE" ]]; then
    echo "Missing virtualenv at $VENV_ACTIVATE"
    echo "Create it first, then install dependencies."
    exit 1
  fi
}

is_running() {
  local pid_file="$1"
  if [[ ! -f "$pid_file" ]]; then
    return 1
  fi
  local pid
  pid="$(cat "$pid_file")"
  if [[ -z "$pid" ]]; then
    return 1
  fi
  kill -0 "$pid" 2>/dev/null
}

start_mcp() {
  if is_running "$MCP_PID_FILE"; then
    echo "MCP server already running (pid $(cat "$MCP_PID_FILE"))."
    return
  fi

  (
    cd "$ROOT_DIR"
    # shellcheck disable=SC1090
    source "$VENV_ACTIVATE"
    nohup python mcp_server.py \
      --transport "$MCP_TRANSPORT" \
      --host "$MCP_HOST" \
      --port "$MCP_PORT" \
      >>"$MCP_LOG_FILE" 2>&1 &
    echo $! >"$MCP_PID_FILE"
  )

  sleep 1
  if is_running "$MCP_PID_FILE"; then
    echo "Started MCP server on ${MCP_HOST}:${MCP_PORT} (pid $(cat "$MCP_PID_FILE"))."
  else
    echo "Failed to start MCP server. Check $MCP_LOG_FILE"
    exit 1
  fi
}

start_webapi() {
  if is_running "$WEB_PID_FILE"; then
    echo "Web API already running (pid $(cat "$WEB_PID_FILE"))."
    return
  fi

  (
    cd "$ROOT_DIR"
    # shellcheck disable=SC1090
    source "$VENV_ACTIVATE"
    nohup uvicorn webapp_api:app \
      --host "$WEBAPI_HOST" \
      --port "$WEBAPI_PORT" \
      >>"$WEB_LOG_FILE" 2>&1 &
    echo $! >"$WEB_PID_FILE"
  )

  sleep 1
  if is_running "$WEB_PID_FILE"; then
    echo "Started Web API on ${WEBAPI_HOST}:${WEBAPI_PORT} (pid $(cat "$WEB_PID_FILE"))."
  else
    echo "Failed to start Web API. Check $WEB_LOG_FILE"
    exit 1
  fi
}

stop_one() {
  local name="$1"
  local pid_file="$2"

  if ! is_running "$pid_file"; then
    rm -f "$pid_file"
    echo "$name not running."
    return
  fi

  local pid
  pid="$(cat "$pid_file")"
  kill "$pid" 2>/dev/null || true

  for _ in {1..20}; do
    if ! kill -0 "$pid" 2>/dev/null; then
      rm -f "$pid_file"
      echo "Stopped $name."
      return
    fi
    sleep 0.25
  done

  echo "$name did not stop gracefully; forcing kill."
  kill -9 "$pid" 2>/dev/null || true
  rm -f "$pid_file"
  echo "Stopped $name."
}

status_one() {
  local name="$1"
  local pid_file="$2"
  if is_running "$pid_file"; then
    echo "$name: running (pid $(cat "$pid_file"))"
  else
    echo "$name: stopped"
  fi
}

show_logs() {
  local target="${1:-all}"
  case "$target" in
    mcp)
      tail -n 100 "$MCP_LOG_FILE"
      ;;
    web|webapi)
      tail -n 100 "$WEB_LOG_FILE"
      ;;
    all)
      echo "== MCP log ($MCP_LOG_FILE) =="
      tail -n 60 "$MCP_LOG_FILE" 2>/dev/null || true
      echo
      echo "== Web API log ($WEB_LOG_FILE) =="
      tail -n 60 "$WEB_LOG_FILE" 2>/dev/null || true
      ;;
    *)
      echo "Unknown log target: $target"
      echo "Use: $0 logs [mcp|web|all]"
      exit 1
      ;;
  esac
}

usage() {
  cat <<EOF
Usage: $0 {start|stop|restart|status|logs [mcp|web|all]}

Environment overrides:
  MCP_TRANSPORT (default: streamable-http)
  MCP_HOST      (default: 127.0.0.1)
  MCP_PORT      (default: 8000)
  WEBAPI_HOST   (default: 0.0.0.0)
  WEBAPI_PORT   (default: 8080)
EOF
}

cmd="${1:-}"
case "$cmd" in
  start)
    require_venv
    start_mcp
    start_webapi
    ;;
  stop)
    stop_one "Web API" "$WEB_PID_FILE"
    stop_one "MCP server" "$MCP_PID_FILE"
    ;;
  restart)
    "$0" stop
    "$0" start
    ;;
  status)
    status_one "MCP server" "$MCP_PID_FILE"
    status_one "Web API" "$WEB_PID_FILE"
    ;;
  logs)
    show_logs "${2:-all}"
    ;;
  *)
    usage
    exit 1
    ;;
esac
