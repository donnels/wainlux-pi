#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
source "$SCRIPT_DIR/lib/container.sh"

if [[ "${1:-}" == "--help" || $# -ne 2 ]]; then
  printf 'Usage: %s IMAGE-NAME localhost/IMAGE:sha-IMAGE-ID\n\nPoint the selected image name back to a retained, previously promoted local version. The replaced image is retained too.\n' "${BASH_SOURCE[0]##*/}"
  if [[ "${1:-}" == "--help" ]]; then exit 0; else exit 2; fi
fi

container_engine
container_restore_image "$1" "$2"
