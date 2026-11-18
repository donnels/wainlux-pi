#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
source "$SCRIPT_DIR/lib/container.sh"

if [[ "${1:-}" == "--help" ]]; then
  printf 'Usage: %s IMAGE-NAME\n\nBuild the Dockerfile in docker/IMAGES.map directly to its selected local tag. Does not scan, version, or deploy the image.\n' "${BASH_SOURCE[0]##*/}"
  exit 0
fi
if [[ $# -ne 1 ]]; then
  echo "Usage: ${BASH_SOURCE[0]##*/} IMAGE-NAME" >&2
  exit 2
fi

container_build_local "$1"
