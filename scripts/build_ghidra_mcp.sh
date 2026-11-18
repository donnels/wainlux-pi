#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
if [[ "${1:-}" == "--help" ]]; then
  printf 'Usage: %s\n\nBuild a candidate Ghidra MCP image, scan it, and promote it only if it passes.\n' "${BASH_SOURCE[0]##*/}"
  exit 0
fi
if [[ $# -ne 0 ]]; then
  echo "Unexpected argument: $1 (use --help for usage)" >&2
  exit 2
fi

exec "$SCRIPT_DIR/update_container_image.sh" ghidra-mcp
