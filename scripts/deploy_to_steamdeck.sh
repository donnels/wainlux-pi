#!/usr/bin/env bash
# Deploy locally on Steam Deck (mock device mode, no laser attached)
# Usage: ./scripts/deploy_to_steamdeck.sh
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(cd "${SCRIPT_DIR}/.." && pwd)"
BASE_ENGINE="${DOCKER_CMD:-podman}"
COMPOSE_DIR="${PROJECT_ROOT}/docker"
COMPOSE_FILE="${COMPOSE_DIR}/compose-dev.yaml"
COMPOSE_PROD_FILE="${COMPOSE_DIR}/compose.yaml"

HOST_SPAWN=""
if [ -n "${FLATPAK_ID:-}" ] || [ -f "/.flatpak-info" ]; then
  if command -v host-spawn >/dev/null 2>&1; then
    HOST_SPAWN="host-spawn"
    echo "Detected Flatpak sandbox; using host-spawn"
  else
    echo "WARNING: Flatpak environment detected but host-spawn is unavailable." >&2
  fi
fi

host_exec() {
  if [ -n "${HOST_SPAWN}" ]; then
    "${HOST_SPAWN}" "$@"
  else
    "$@"
  fi
}

COMPOSE_MODE=""
COMPOSE_ENGINE=""
COMPOSE_BIN=""
if host_exec "${BASE_ENGINE}" compose version >/dev/null 2>&1; then
  COMPOSE_MODE="engine"
  COMPOSE_ENGINE="${BASE_ENGINE}"
elif host_exec docker compose version >/dev/null 2>&1; then
  COMPOSE_MODE="engine"
  COMPOSE_ENGINE="docker"
elif [ -x "${HOME}/venv/bin/podman-compose" ] && host_exec "${HOME}/venv/bin/podman-compose" --version >/dev/null 2>&1; then
  COMPOSE_MODE="binary"
  COMPOSE_BIN="${HOME}/venv/bin/podman-compose"
elif host_exec podman-compose --version >/dev/null 2>&1; then
  COMPOSE_MODE="binary"
  COMPOSE_BIN="podman-compose"
else
  echo "ERROR: No compose support found (tried ${BASE_ENGINE} compose, docker compose, podman-compose)." >&2
  exit 1
fi

compose() {
  if [ "${COMPOSE_MODE}" = "engine" ]; then
    host_exec "${COMPOSE_ENGINE}" compose "$@"
  else
    host_exec "${COMPOSE_BIN}" "$@"
  fi
}

port_in_use() {
  port="$1"
  if host_exec ss -ltnH >/dev/null 2>&1; then
    host_exec ss -ltnH | awk -v p=":${port}" '$4 ~ p"$" {found=1} END {exit !found}'
    return $?
  fi
  if host_exec sh -lc "command -v lsof >/dev/null 2>&1"; then
    host_exec lsof -nP -iTCP:"${port}" -sTCP:LISTEN >/dev/null 2>&1
    return $?
  fi
  return 1
}

port_pids() {
  port="$1"
  pids=""
  if host_exec ss -ltnpH >/dev/null 2>&1; then
    pids="$(host_exec ss -ltnpH | awk -v p=":${port}" '
      $4 ~ p"$" {
        while (match($0, /pid=[0-9]+/)) {
          print substr($0, RSTART + 4, RLENGTH - 4)
          $0 = substr($0, RSTART + RLENGTH)
        }
      }' | sort -u)"
  fi
  if [ -z "${pids}" ] && host_exec sh -lc "command -v fuser >/dev/null 2>&1"; then
    pids="$(host_exec fuser "${port}/tcp" 2>/dev/null | tr ' ' '\n' | awk '/^[0-9]+$/')"
  fi
  [ -n "${pids}" ] && echo "${pids}"
}

kill_port_pids() {
  port="$1"
  pids="$(port_pids "${port}" || true)"
  [ -z "${pids}" ] && return 0
  for pid in ${pids}; do
    host_exec kill -TERM "${pid}" >/dev/null 2>&1 || true
  done
  sleep 1
  if port_in_use "${port}"; then
    for pid in ${pids}; do
      host_exec kill -KILL "${pid}" >/dev/null 2>&1 || true
    done
  fi
}

looks_like_container_port_helper() {
  port="$1"
  pids="$(port_pids "${port}" || true)"
  [ -z "${pids}" ] && return 1
  for pid in ${pids}; do
    line="$(host_exec ps -p "${pid}" -o comm= -o args= 2>/dev/null || true)"
    echo "${line}" | grep -Eqi 'rootlessport|podman|conmon|slirp4netns' && return 0
  done
  return 1
}

cleanup_wainlux_on_port() {
  port="$1"
  runtime=""
  if host_exec podman ps --format '{{.ID}}' >/dev/null 2>&1; then
    runtime="podman"
  elif host_exec docker ps --format '{{.ID}}' >/dev/null 2>&1; then
    runtime="docker"
  fi
  [ -z "${runtime}" ] && return 0

  ids="$(host_exec "${runtime}" ps -a --format '{{.ID}} {{.Names}} {{.Image}} {{.Ports}}' | awk -v p=":${port}->" 'index($0, p) {line=tolower($0); if (line ~ /wainlux|k6|mcp|docker_wainlux/) print $1}')"
  [ -z "${ids}" ] && return 0

  echo "  Found Wainlux container(s) on :${port}; removing..."
  for id in ${ids}; do
    host_exec "${runtime}" rm -f "${id}" >/dev/null 2>&1 || true
  done
}

looks_like_wainlux_service() {
  port="$1"
  host_exec sh -lc "command -v curl >/dev/null 2>&1" || return 1

  if [ "${port}" = "8080" ]; then
    host_exec curl -sS --max-time 2 "http://127.0.0.1:${port}/api/status" 2>/dev/null | grep -Eqi 'connected|mock|k6|wainlux'
    return $?
  fi

  if [ "${port}" = "8081" ]; then
    code="$(host_exec curl -sS --max-time 2 -o /dev/null -w '%{http_code}' "http://127.0.0.1:${port}/mcp" 2>/dev/null || true)"
    case "${code}" in
      200|400|401|403|404|405) return 0 ;;
    esac
    host_exec curl -sS --max-time 2 "http://127.0.0.1:${port}/" 2>/dev/null | grep -Eqi 'mcp|k6|wainlux'
    return $?
  fi

  return 1
}

ensure_port_available() {
  port="$1"
  if ! port_in_use "${port}"; then
    return 0
  fi

  echo "  Port ${port} is in use; checking for Wainlux containers..."
  cleanup_wainlux_on_port "${port}"
  if ! port_in_use "${port}"; then
    return 0
  fi

  if looks_like_wainlux_service "${port}"; then
    echo "  Port ${port} serves Wainlux content; terminating listener..."
    kill_port_pids "${port}"
  fi

  if port_in_use "${port}" && looks_like_container_port_helper "${port}"; then
    echo "  Port ${port} is held by container port helper; terminating stale listener..."
    kill_port_pids "${port}"
  fi

  if port_in_use "${port}"; then
    echo "ERROR: Port ${port} is still in use by a non-Wainlux service. Free it, then retry." >&2
    exit 1
  fi
}

echo "=== Wainlux K6 Steam Deck (Local Mock) ==="
echo "Root: ${PROJECT_ROOT}"
echo "Compose: ${COMPOSE_FILE}"
if [ "${COMPOSE_MODE}" = "engine" ]; then
  echo "Engine: ${COMPOSE_ENGINE} (compose plugin)"
else
  echo "Compose binary: ${COMPOSE_BIN}"
fi
echo

if [ ! -f "${COMPOSE_FILE}" ]; then
  echo "ERROR: compose-dev.yaml not found at ${COMPOSE_FILE}" >&2
  exit 1
fi

echo "[1/6] System check (Docker/Podman)..."
if [ "${COMPOSE_MODE}" = "engine" ]; then
  host_exec "${COMPOSE_ENGINE}" --version
else
  host_exec "${BASE_ENGINE}" --version || true
  host_exec "${COMPOSE_BIN}" --version
fi

echo
echo "[2/6] Preparing data directory..."
mkdir -p "${COMPOSE_DIR}/docker-wainlux/data"

echo
echo "[3/6] Stopping previous stack..."
(
  cd "${COMPOSE_DIR}"
  compose -f "${COMPOSE_FILE}" down --remove-orphans || true
  compose -f "${COMPOSE_PROD_FILE}" down --remove-orphans || true
)

echo
echo "[4/6] Ensuring required ports are available..."
ensure_port_available 8080
ensure_port_available 8082

echo
echo "[5/6] Building image in mock mode (compose-dev)..."
(
  cd "${COMPOSE_DIR}"
  compose -f "${COMPOSE_FILE}" build
)

echo
echo "[6/6] Starting service and checking logs..."
(
  cd "${COMPOSE_DIR}"
  compose -f "${COMPOSE_FILE}" up -d
  sleep 2
  compose -f "${COMPOSE_FILE}" ps
  compose -f "${COMPOSE_FILE}" logs --tail=20
)

if command -v curl >/dev/null 2>&1; then
  echo
  echo "[smoke] GET /api/status"
  if ! curl -fsS "http://localhost:8080/api/status" >/dev/null; then
    echo "WARNING: /api/status smoke test failed" >&2
  fi
fi

echo
echo "=== Mock Deployment Complete ==="
echo "Web interface: http://localhost:8080/"
echo "MCP endpoint:  http://localhost:8082/mcp"
echo
if [ "${COMPOSE_MODE}" = "engine" ]; then
  COMPOSE_HINT="${COMPOSE_ENGINE} compose"
else
  COMPOSE_HINT="${COMPOSE_BIN}"
fi
echo "To view logs: cd ${COMPOSE_DIR} && ${COMPOSE_HINT} -f ${COMPOSE_FILE} logs -f"
echo "To stop:      cd ${COMPOSE_DIR} && ${COMPOSE_HINT} -f ${COMPOSE_FILE} down"
echo
