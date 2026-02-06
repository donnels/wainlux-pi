#!/usr/bin/env bash
# Deploy locally on Steam Deck (mock device mode, no laser attached)
# Usage: ./scripts/deploy_to_steamdeck.sh
# Auto-detects Flatpak sandbox and uses host-spawn if needed
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(cd "${SCRIPT_DIR}/.." && pwd)"
BASE_DOCKER_CMD="${DOCKER_CMD:-podman}"
DOCKER_DIR="${PROJECT_ROOT}/docker-wainlux"
COMPOSE_FILE="${DOCKER_DIR}/compose-dev.yaml"  # K6_MOCK_DEVICE=true
SERVICE_NAME="wainlux-k6-dev"

# Detect Flatpak sandbox (VS Code Flatpak sets FLATPAK_ID)
HOST_SPAWN=""
if [ -n "${FLATPAK_ID:-}" ] || [ -f "/.flatpak-info" ]; then
  if command -v host-spawn >/dev/null 2>&1; then
    HOST_SPAWN="host-spawn"
    echo "Detected Flatpak sandbox; using host-spawn for container commands"
  else
    echo "WARNING: Running in Flatpak but host-spawn not found. Install: flatpak install com.visualstudio.code.tool.podman" >&2
  fi
fi

# Resolve compose command (check without host-spawn, then prefix)
COMPOSE_BIN=""
if [ -n "${HOST_SPAWN}" ]; then
  # In Flatpak: check host commands via host-spawn
  if ${HOST_SPAWN} ${BASE_DOCKER_CMD} compose version >/dev/null 2>&1; then
    COMPOSE_BIN="${HOST_SPAWN} ${BASE_DOCKER_CMD} compose"
  elif [ -f "${HOME}/venv/bin/podman-compose" ]; then
    # Check for venv install (common on Steam Deck)
    COMPOSE_BIN="${HOST_SPAWN} ${HOME}/venv/bin/podman-compose"
  elif ${HOST_SPAWN} podman-compose --version >/dev/null 2>&1; then
    COMPOSE_BIN="${HOST_SPAWN} podman-compose"
  elif ${HOST_SPAWN} docker compose version >/dev/null 2>&1; then
    COMPOSE_BIN="${HOST_SPAWN} docker compose"
  fi
else
  # Native: check local commands
  if ${BASE_DOCKER_CMD} compose version >/dev/null 2>&1; then
    COMPOSE_BIN="${BASE_DOCKER_CMD} compose"
  elif [ -f "${HOME}/venv/bin/podman-compose" ]; then
    # Check for venv install (common on Steam Deck)
    COMPOSE_BIN="${HOME}/venv/bin/podman-compose"
  elif command -v podman-compose >/dev/null 2>&1; then
    COMPOSE_BIN="podman-compose"
  elif command -v docker >/dev/null 2>&1 && docker compose version >/dev/null 2>&1; then
    COMPOSE_BIN="docker compose"
  fi
fi

if [ -z "${COMPOSE_BIN}" ]; then
  echo "ERROR: No compose support found (tried ${BASE_DOCKER_CMD} compose / podman-compose / docker compose)." >&2
  echo "" >&2
  echo "To install podman-compose on Steam Deck:" >&2
  echo "  ${HOST_SPAWN:+${HOST_SPAWN} }python -m pip install --user podman-compose" >&2
  echo "" >&2
  echo "Or install docker-compose:" >&2
  echo "  ${HOST_SPAWN:+${HOST_SPAWN} }curl -SL https://github.com/docker/compose/releases/latest/download/docker-compose-linux-x86_64 -o ~/.docker/cli-plugins/docker-compose" >&2
  echo "  ${HOST_SPAWN:+${HOST_SPAWN} }chmod +x ~/.docker/cli-plugins/docker-compose" >&2
  exit 1
fi

# Finalize DOCKER_CMD with host-spawn prefix if needed
DOCKER_CMD="${HOST_SPAWN:+${HOST_SPAWN} }${BASE_DOCKER_CMD}"

echo "=== Wainlux K6 Steam Deck (Local Mock) ==="
echo "Root: ${PROJECT_ROOT}"
echo "Compose: ${COMPOSE_FILE}"
echo "Docker CLI: ${DOCKER_CMD} (compose via '${COMPOSE_BIN}')"
echo

if [ ! -f "${COMPOSE_FILE}" ]; then
  echo "ERROR: compose-dev.yaml not found at ${COMPOSE_FILE}" >&2
  exit 1
fi

echo "[1/5] System check (Docker/Podman)..."
${DOCKER_CMD} --version

echo
echo "[2/5] Preparing data directory..."
mkdir -p "${DOCKER_DIR}/data"

echo
echo "[3/5] Cleaning previous runs (containers using :8080)..."
set +e
${COMPOSE_BIN} -f "${COMPOSE_FILE}" down --remove-orphans --volumes || true
# Stop and remove any container publishing 8080 (Podman/Docker compatible)
PUBLISH_FILTER=$(${DOCKER_CMD} ps -a --format '{{.ID}} {{.Ports}}' | grep -E '(:|->)8080' | awk '{print $1}')
if [ -n "${PUBLISH_FILTER}" ]; then
  echo "  Stopping containers on 8080: ${PUBLISH_FILTER}"
  echo "${PUBLISH_FILTER}" | xargs -r ${DOCKER_CMD} stop || true
  echo "${PUBLISH_FILTER}" | xargs -r ${DOCKER_CMD} rm || true
fi
# Remove default compose network if it lingers
${DOCKER_CMD} network rm docker-wainlux_default 2>/dev/null || true
set -e

echo
echo "[4/5] Building image in mock mode (compose-dev)..."
cd "${DOCKER_DIR}"
${COMPOSE_BIN} -f "${COMPOSE_FILE}" build

echo
echo "[5/5] Starting service..."
${COMPOSE_BIN} -f "${COMPOSE_FILE}" up -d
sleep 2
${COMPOSE_BIN} -f "${COMPOSE_FILE}" ps

echo
echo "=== Mock Deployment Complete ==="
echo "Web interface: http://localhost:8080/"
echo "Logs: ${COMPOSE_BIN} -f ${COMPOSE_FILE} logs -f"
echo "Stop: ${COMPOSE_BIN} -f ${COMPOSE_FILE} down"
echo
