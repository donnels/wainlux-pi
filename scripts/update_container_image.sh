#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
source "$SCRIPT_DIR/lib/container.sh"

if [[ "${1:-}" == "--help" ]]; then
  printf 'Usage: %s [--rescan] [--allow-findings] IMAGE-NAME\n\nBuild and scan a candidate, then promote it. Findings fail by default. --allow-findings records the report and Dockerfile in that image README, then permits promotion.\nIMAGE-NAME is listed in docker/IMAGES.map. The current selected image stays available if update fails.\n' "${BASH_SOURCE[0]##*/}"
  exit 0
fi

FORCE_SCAN=""
ALLOW_FINDINGS=""
while [[ "${1:-}" == --* ]]; do
  case "$1" in
    --rescan) FORCE_SCAN="--rescan" ;;
    --allow-findings) ALLOW_FINDINGS="--allow-findings" ;;
    *) echo "Unknown option: $1" >&2; exit 2 ;;
  esac
  shift
done
if [[ $# -ne 1 ]]; then
  printf 'Usage: %s [--rescan] IMAGE-NAME\n' "${BASH_SOURCE[0]##*/}" >&2
  exit 2
fi

container_update_image "$1" "$FORCE_SCAN" "$ALLOW_FINDINGS"
