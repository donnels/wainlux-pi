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
    asciidoctor --failure-level=WARN "$@"
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
mapfile -t DOCS < <(find "${PROJECT_ROOT}" \( -name .git -o -name .venv -o -name docs -o -name backup -o -name history \) -prune -o -type f \( -name "*.adoc" -o -name "*.asciidoc" \) -print | sort)

echo "Found ${#DOCS[@]} AsciiDoc files"
echo

# Test each document standalone
for doc in "${DOCS[@]}"; do
  TOTAL=$((TOTAL + 1))
  rel_path="${doc#${PROJECT_ROOT}/}"
  basename=$(basename "$doc")
  
  # Try to render with minimal attributes
  # data-uri embeds images, safe mode allows includes
  if [[ "${basename}" == "README.asciidoc" ]]; then
    echo "[ SKIP ] ${rel_path} (validated as the single-page build below)"
    SKIPPED=$((SKIPPED + 1))
    continue
  fi

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
echo "=== Block Caption Test ==="
for doc in "${DOCS[@]}"; do
  rel_path="${doc#${PROJECT_ROOT}/}"
  missing_captions=$(awk 'FNR == 1 { previous = "" } /^(image::|\[source|\[listing\])/ && previous !~ /^\./ { print FNR ": " $0 } { previous = $0 }' "${doc}")
  if [[ -n "${missing_captions}" ]]; then
    echo "[ FAIL ] ${rel_path} has uncaptained image/source/listing blocks"
    printf '%s\n' "${missing_captions}"
    FAILED=$((FAILED + 1))
  fi
done
if [[ "${FAILED}" -eq 0 ]]; then
  echo "[ PASS ] Every image, source block, and listing has a block caption"
fi

mapfile -t PLANTUML_FILES < <(find "${PROJECT_ROOT}" \( -name .git -o -name target -o -name .venv \) -prune -o -type f -name "*.plantuml" -print | sort)
echo
echo "=== PlantUML Attribution Test ==="
for source in "${PLANTUML_FILES[@]}"; do
  rel_path="${source#${PROJECT_ROOT}/}"
  if grep -Fqx 'footer (C)2026 Sean Donnellan' "${source}"; then
    echo "[ PASS ] ${rel_path}"
  else
    echo "[ FAIL ] ${rel_path} (missing exact attribution footer)"
    FAILED=$((FAILED + 1))
  fi
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
    -a single-page \
    -o "${TEMP_DIR}/README.html" \
    "${PROJECT_ROOT}/README.asciidoc" 2>"${TEMP_DIR}/main-errors.txt"; then
    
    if [[ -f "${TEMP_DIR}/README.html" && -s "${TEMP_DIR}/README.html" ]]; then
      echo "[ PASS ] Main document builds successfully"
      size=$(stat -f%z "${TEMP_DIR}/README.html" 2>/dev/null || stat -c%s "${TEMP_DIR}/README.html" 2>/dev/null || echo "unknown")
      echo "Output: ${size} bytes"

      image_count=$(grep -o '<img[[:space:]][^>]*>' "${TEMP_DIR}/README.html" | wc -l)
      inline_image_count=$(grep -o '<img[[:space:]][^>]*src="data:image/[^\"]*"[^>]*>' "${TEMP_DIR}/README.html" | wc -l)
      if [[ "${image_count}" -gt 0 && "${image_count}" -eq "${inline_image_count}" ]]; then
        echo "[ PASS ] All ${image_count} images are embedded as data URIs"
      else
        echo "[ FAIL ] ${image_count} images found; only ${inline_image_count} are embedded as data URIs"
        FAILED=$((FAILED + 1))
      fi

      non_self_contained_urls=$(grep -Eo '(href|src)="[^"]*"' "${TEMP_DIR}/README.html" | sed -E 's/^(href|src)="([^"]*)"$/\2/' | grep -Ev '^(#|data:|https?://|mailto:|tel:)' || true)
      if [[ -n "${non_self_contained_urls}" ]]; then
        echo "[ FAIL ] Generated HTML contains non-embedded or relative href/src values"
        printf '%s\n' "${non_self_contained_urls}" | head -n 10
        FAILED=$((FAILED + 1))
      else
        echo "[ PASS ] No relative href or src in generated HTML"
      fi

      if grep -q 'href="#api-contract"' "${TEMP_DIR}/README.html" && grep -q 'id="api-contract"' "${TEMP_DIR}/README.html"; then
        echo "[ PASS ] API contract cross-reference resolves inside this HTML"
      else
        echo "[ FAIL ] API contract cross-reference is missing its included target"
        FAILED=$((FAILED + 1))
      fi

      if grep -q 'Permission is hereby granted' "${TEMP_DIR}/README.html" && grep -q 'Creative Commons Zero v1.0' "${TEMP_DIR}/README.html"; then
        echo "[ PASS ] Code and documentation license texts are embedded"
      else
        echo "[ FAIL ] A full license text is missing from the single-page HTML"
        FAILED=$((FAILED + 1))
      fi
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
