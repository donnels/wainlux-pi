#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
IMAGE="localhost/laser-app:arm"
TARGET="${LASER_SSH_TARGET:-}"
CONFIRM=false

source "$SCRIPT_DIR/lib/container.sh"

while [[ $# -gt 0 ]]; do
  case "$1" in
    --help)
      printf 'Usage: %s --target USER@PI-HOST --confirm\n\nTransfers %s from this host and recreates the idle Pi Zero W service.\nSet LASER_SSH_TARGET instead of --target to configure a default destination.\nSet CONTAINER_ENGINE=docker or podman to override engine detection.\n' "${BASH_SOURCE[0]##*/}" "$IMAGE"
      exit 0
      ;;
    --target)
      if [[ $# -lt 2 || -z "$2" ]]; then echo "--target requires USER@PI-HOST." >&2; exit 2; fi
      TARGET="$2"
      shift 2
      ;;
    --confirm)
      CONFIRM=true
      shift
      ;;
    *) echo "Unknown argument: $1 (use --help)." >&2; exit 2 ;;
  esac
done

if [[ -z "$TARGET" ]]; then
  echo "Set LASER_SSH_TARGET or pass --target USER@PI-HOST." >&2
  exit 2
fi
if [[ "$CONFIRM" != true ]]; then
  echo "Deployment restarts the laser service. Review it first, then pass --confirm." >&2
  exit 2
fi
container_engine || { echo "Start Docker or Podman on the host before deploying." >&2; exit 1; }

LOCAL_IMAGE_ID="$(container_host image inspect "$IMAGE" --format '{{.Id}}')"
LOCAL_IMAGE_ID="${LOCAL_IMAGE_ID#sha256:}"
LOCAL_PLATFORM="$(container_host image inspect "$IMAGE" --format '{{.Os}}/{{.Architecture}}')"
if [[ "$LOCAL_PLATFORM" != "linux/arm" ]]; then
  echo "Expected an ARM image, found $LOCAL_PLATFORM. Run ./scripts/build_laser.sh first." >&2
  exit 1
fi

echo "Checking $TARGET before deployment..."
ssh_host "$TARGET" '
set -eu
model=$(tr -d "\000" </proc/device-tree/model)
if [ "$model" != "Raspberry Pi Zero W Rev 1.1" ] || [ "$(uname -m)" != "armv6l" ]; then
  echo "Refusing deployment: expected Raspberry Pi Zero W Rev 1.1 (armv6l), found $model ($(uname -m))." >&2
  exit 1
fi
test -f ~/laser-app/compose.yaml
docker inspect laser-app >/dev/null
curl -fsS http://127.0.0.1:8081/api/state | python3 -c "import json,sys; s=json.load(sys.stdin); m=s.get(\"mode\") or {}; ok=s.get(\"running\") is False and s.get(\"attempt\") is None and s.get(\"outcome\") is None and s.get(\"review_id\") is None and m.get(\"real\") is False; print(\"API idle; Simulator mode; no attempt, outcome, or review\" if ok else \"Refusing deployment: service must be idle in Simulator mode with no attempt, outcome, or review\", file=sys.stdout if ok else sys.stderr); sys.exit(0 if ok else 1)"
'

REMOTE_IMAGE_ID="$(ssh_host "$TARGET" 'docker image inspect --format "{{.Id}}" localhost/laser-app:arm 2>/dev/null || true')"
REMOTE_IMAGE_ID="${REMOTE_IMAGE_ID#sha256:}"
CONTAINER_IMAGE_ID="$(ssh_host "$TARGET" 'docker inspect --format "{{.Image}}" laser-app')"
CONTAINER_IMAGE_ID="${CONTAINER_IMAGE_ID#sha256:}"
if [[ "$REMOTE_IMAGE_ID" != "$LOCAL_IMAGE_ID" ]]; then
  echo "Streaming the workstation-built image to $TARGET..."
  container_host save "$IMAGE" | gzip -1 | ssh_host "$TARGET" 'gunzip | docker load'
  REMOTE_IMAGE_ID="$(ssh_host "$TARGET" 'docker image inspect --format "{{.Id}}" localhost/laser-app:arm')"
  REMOTE_IMAGE_ID="${REMOTE_IMAGE_ID#sha256:}"
  if [[ "$REMOTE_IMAGE_ID" != "$LOCAL_IMAGE_ID" ]]; then
    echo "Loaded image ID does not match the Deck build; refusing to restart." >&2
    exit 1
  fi
fi
if [[ "$CONTAINER_IMAGE_ID" != "$LOCAL_IMAGE_ID" ]]; then
  echo "Recreating the Pi service from the verified image..."
  ssh_host "$TARGET" 'cd ~/laser-app && docker compose up -d --force-recreate laser-app'
else
  echo "The verified image is already loaded and running; no transfer or restart needed."
fi

ssh_host "$TARGET" 'docker inspect --format "Container={{.Name}} Image={{.Image}} Running={{.State.Running}}" laser-app; curl -fsS http://127.0.0.1:8081/api/state | python3 -c "import json,sys; s=json.load(sys.stdin); m=s.get(\"mode\") or {}; ok=s.get(\"running\") is False and s.get(\"attempt\") is None and s.get(\"outcome\") is None and s.get(\"review_id\") is None and m.get(\"real\") is False; print(\"Service ready in Simulator mode; no attempt or review\" if ok else \"Unexpected service state after deploy\", file=sys.stdout if ok else sys.stderr); sys.exit(0 if ok else 1)"'
echo "Deployment verified. No laser job was started."