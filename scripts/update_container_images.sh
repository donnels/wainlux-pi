#!/usr/bin/env bash
set -uo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
source "$SCRIPT_DIR/lib/container.sh"

if [[ "${1:-}" == "--help" ]]; then
  printf 'Usage: %s [--rescan] [--allow-findings] [IMAGE-NAME ...]\n\nBuild, scan, and promote selected images. With no names, processes every scannable image except ghidra-mcp. Findings fail by default. --allow-findings records each report and Dockerfile in the image README, then permits promotion.\n' "${BASH_SOURCE[0]##*/}"
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

container_engine || exit $?

if [[ $# -gt 0 ]]; then
  IMAGES=("$@")
else
  IMAGES=()
  while IFS='|' read -r name _; do
    [[ -z "${name:-}" || "$name" == \#* ]] && continue
    [[ "$name" == "ghidra-mcp" ]] && continue
    IMAGES+=("$name")
  done < "$REPO_ROOT/docker/IMAGES.map"
fi

failed=0
for name in "${IMAGES[@]}"; do
  echo
  echo "=== Updating $name ==="
  if ! container_update_image "$name" "$FORCE_SCAN" "$ALLOW_FINDINGS"; then
    echo "Update failed for $name; its previous promoted image remains selected." >&2
    failed=1
  fi
done

exit "$failed"
