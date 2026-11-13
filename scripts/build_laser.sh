#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
SERVICE_DIR="$REPO_ROOT/docker/docker-wainlux-rs"
IMAGE="localhost/laser-app:arm"
RUST_IMAGE="docker.io/library/rust@sha256:9daf2b244ccc30523e55cafe32edf8465671ba567d0101620e5612ccb76ea1a7"
CARGO_CACHE="wainlux-pi-cargo-registry"
CARGO_TARGET="wainlux-pi-cargo-target"

source "$SCRIPT_DIR/lib/container.sh"

if [[ "${1:-}" == "--help" ]]; then
  printf 'Usage: %s\n\nRuns the locked CLI and Rust service tests, then builds %s for linux/arm/v6.\nThe image stays in the local container engine; nothing is deployed to the Pi.\nSet CONTAINER_ENGINE=docker or podman to override automatic detection.\n' "${BASH_SOURCE[0]##*/}" "$IMAGE"
  exit 0
fi
if [[ $# -ne 0 ]]; then
  echo "Unexpected argument: $1 (use --help for usage)" >&2
  exit 2
fi

if ! container_engine; then
  echo "Start Docker or Podman on the host. Flatpak host access is handled automatically when available." >&2
  exit 1
fi

echo "Using $CONTAINER_ENGINE_SELECTED on the host."
for manifest in cli/Cargo.toml docker/docker-wainlux-rs/Cargo.toml; do
  echo "Testing $manifest in $RUST_IMAGE..."
  container_host run --rm \
    -v "$REPO_ROOT:/repo" \
    -v "$CARGO_CACHE:/usr/local/cargo/registry" \
    -v "$CARGO_TARGET:/target" \
    -w /repo \
    -e CARGO_TARGET_DIR=/target \
    "$RUST_IMAGE" cargo test --workspace --locked --manifest-path "$manifest"
done

echo "Building $IMAGE for linux/arm/v6..."
container_host build \
  --platform linux/arm/v6 \
  --tag "$IMAGE" \
  "$SERVICE_DIR"

echo "Built $IMAGE with $CONTAINER_ENGINE_SELECTED. No image transfer or Pi deployment was performed."