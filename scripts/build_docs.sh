#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
source "$SCRIPT_DIR/lib/container.sh"
container_engine
IMAGE="$(container_require_image asciidoctor)"

mkdir -p "$REPO_ROOT/docs"
cp "$REPO_ROOT/robots.txt" "$REPO_ROOT/docs/robots.txt"

container_host run --rm \
  -v "$REPO_ROOT:/repo" -w /repo \
  --entrypoint asciidoctor "$IMAGE" \
  -a allow-uri-read -a data-uri -a single-page \
  -o docs/README.html README.asciidoc

container_host run --rm \
  -v "$REPO_ROOT:/repo" -w /repo \
  --entrypoint asciidoctor-pdf "$IMAGE" \
  -a allow-uri-read -a data-uri \
  -o docs/README.pdf README.asciidoc
