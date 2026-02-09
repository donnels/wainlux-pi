#!/usr/bin/env bash
# Deploy locally on Steam Deck (mock device mode, no laser attached)
# Usage: ./scripts/deploy_to_steamdeck.sh
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(cd "${SCRIPT_DIR}/.." && pwd)"
BASE_ENGINE="${DOCKER_CMD:-podman}"
DOCKER_DIR="${PROJECT_ROOT}/docker-wainlux"
COMPOSE_FILE="${DOCKER_DIR}/compose-dev.yaml"

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

echo "[1/5] System check (Docker/Podman)..."
if [ "${COMPOSE_MODE}" = "engine" ]; then
  host_exec "${COMPOSE_ENGINE}" --version
else
  host_exec "${BASE_ENGINE}" --version || true
  host_exec "${COMPOSE_BIN}" --version
fi

echo
echo "[2/5] Preparing data directory..."
mkdir -p "${DOCKER_DIR}/data"

echo
echo "[3/5] Stopping previous stack..."
(
  cd "${DOCKER_DIR}"
  compose -f "${COMPOSE_FILE}" down --remove-orphans || true
)

echo
echo "[4/5] Building image in mock mode (compose-dev)..."
(
  cd "${DOCKER_DIR}"
  compose -f "${COMPOSE_FILE}" build
)

echo
echo "[5/5] Starting service and checking logs..."
(
  cd "${DOCKER_DIR}"
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
echo "To view logs: cd ${DOCKER_DIR} && ${COMPOSE_HINT} -f ${COMPOSE_FILE} logs -f"
echo "To stop:      cd ${DOCKER_DIR} && ${COMPOSE_HINT} -f ${COMPOSE_FILE} down"
echo
