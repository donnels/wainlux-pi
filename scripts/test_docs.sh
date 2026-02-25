#!/usr/bin/env bash
# Test all AsciiDoc files for rendering consistency
# Validates standalone and included rendering contexts
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(cd "${SCRIPT_DIR}/.." && pwd)"
TEMP_DIR="${PROJECT_ROOT}/.test-docs-tmp"
mkdir -p "${TEMP_DIR}"
trap 'rm -rf "${TEMP_DIR}"' EXIT

# Detect host-spawn for Flatpak environments
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

# Check for podman
if ! host_exec podman --version >/dev/null 2>&1; then
  echo "ERROR: podman not found. Install with:"
  echo "  sudo apt install podman (Debian/Ubuntu)"
  echo "  brew install podman (macOS)"
  echo "  or use Docker: docker run --rm -v ... asciidoctor/docker-asciidoctor"
  exit 1
fi

# Asciidoctor wrapper using container
asciidoctor() {
  host_exec podman run --rm \
    -v "${PROJECT_ROOT}:${PROJECT_ROOT}:Z" \
    -w "${PROJECT_ROOT}" \
    asciidoctor/docker-asciidoctor \
    asciidoctor "$@"
}

echo "=== AsciiDoc Consistency Test ==="
echo "Project: ${PROJECT_ROOT}"
echo "Container: asciidoctor/docker-asciidoctor via ${HOST_SPAWN:-native} podman"
echo

TOTAL=0
PASSED=0
FAILED=0
SKIPPED=0

# Find all .adoc and .asciidoc files
mapfile -t DOCS < <(find "${PROJECT_ROOT}" -type f \( -name "*.adoc" -o -name "*.asciidoc" \) | grep -v '.git' | sort)

echo "Found ${#DOCS[@]} AsciiDoc files"
echo

# Skip list: files that are include-only and should not render standalone
SKIP_PATTERNS=(
  "include-formatting-"
  "include-glossary"
  "include-license"
  "include-steamdeck"
)

should_skip() {
  local file="$1"
  local basename=$(basename "$file")
  
  for pattern in "${SKIP_PATTERNS[@]}"; do
    if [[ "$basename" == *"$pattern"* ]]; then
      return 0
    fi
  done
  return 1
}

# Test each document standalone
for doc in "${DOCS[@]}"; do
  TOTAL=$((TOTAL + 1))
  rel_path="${doc#${PROJECT_ROOT}/}"
  
  if should_skip "$doc"; then
    echo "[ SKIP ] ${rel_path} (include-only file)"
    SKIPPED=$((SKIPPED + 1))
    continue
  fi
  
  # Try to render with minimal attributes
  # data-uri embeds images, safe mode allows includes
  if asciidoctor \
    -a data-uri \
    -a allow-uri-read \
    -a github-env \
    -o "${TEMP_DIR}/test.html" \
    "${doc}" 2>"${TEMP_DIR}/errors.txt"; then
    
    # Check if output was created
    if [[ -f "${TEMP_DIR}/test.html" && -s "${TEMP_DIR}/test.html" ]]; then
      echo "[ PASS ] ${rel_path}"
      PASSED=$((PASSED + 1))
    else
      echo "[ FAIL ] ${rel_path} (empty output)"
      FAILED=$((FAILED + 1))
      cat "${TEMP_DIR}/errors.txt" 2>/dev/null || true
    fi
  else
    echo "[ FAIL ] ${rel_path} (asciidoctor error)"
    FAILED=$((FAILED + 1))
    cat "${TEMP_DIR}/errors.txt" 2>/dev/null || true
  fi
  
  rm -f "${TEMP_DIR}/test.html" "${TEMP_DIR}/errors.txt"
done

echo
echo "=== Summary ==="
echo "Total:   ${TOTAL}"
echo "Passed:  ${PASSED}"
echo "Failed:  ${FAILED}"
echo "Skipped: ${SKIPPED}"
echo

# Test main document with includes
if [[ -f "${PROJECT_ROOT}/README.asciidoc" ]]; then
  echo "=== Testing Main Document with Includes ==="
  if asciidoctor \
    -a data-uri \
    -a allow-uri-read \
    -a github-env \
    -o "${TEMP_DIR}/README.html" \
    "${PROJECT_ROOT}/README.asciidoc" 2>"${TEMP_DIR}/main-errors.txt"; then
    
    if [[ -f "${TEMP_DIR}/README.html" && -s "${TEMP_DIR}/README.html" ]]; then
      echo "[ PASS ] Main document builds successfully"
      size=$(stat -f%z "${TEMP_DIR}/README.html" 2>/dev/null || stat -c%s "${TEMP_DIR}/README.html" 2>/dev/null || echo "unknown")
      echo "Output: ${size} bytes"
    else
      echo "[ FAIL ] Main document produced empty output"
      cat "${TEMP_DIR}/main-errors.txt" 2>/dev/null || true
      FAILED=$((FAILED + 1))
    fi
  else
    echo "[ FAIL ] Main document failed to build"
    cat "${TEMP_DIR}/main-errors.txt" 2>/dev/null || true
    FAILED=$((FAILED + 1))
  fi
fi

echo

if [[ $FAILED -gt 0 ]]; then
  echo "RESULT: FAILED (${FAILED} failures)"
  exit 1
else
  echo "RESULT: PASSED (all tests successful)"
  exit 0
fi
