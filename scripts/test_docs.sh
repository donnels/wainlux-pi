#!/usr/bin/env bash
# Test all AsciiDoc files for rendering consistency
# Validates standalone and included rendering contexts
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(cd "${SCRIPT_DIR}/.." && pwd)"
TEMP_DIR="${PROJECT_ROOT}/.test-docs-tmp"
source "${SCRIPT_DIR}/lib/container.sh"
mkdir -p "${TEMP_DIR}"
trap 'rm -rf "${TEMP_DIR}"' EXIT

if ! container_engine; then
  echo "ERROR: start a Docker or Podman engine on the host before running documentation checks." >&2
  exit 1
fi

# Asciidoctor wrapper using container
asciidoctor() {
  container_host run --rm \
    -v "${PROJECT_ROOT}:${PROJECT_ROOT}" \
    -w "${PROJECT_ROOT}" \
    asciidoctor/docker-asciidoctor \
    asciidoctor --failure-level=WARN "$@"
}

echo "=== AsciiDoc Consistency Test ==="
echo "Project: ${PROJECT_ROOT}"
echo "Container engine: ${CONTAINER_ENGINE_SELECTED}"
echo

TOTAL=0
PASSED=0
FAILED=0
SKIPPED=0

# Find all .adoc and .asciidoc files
DOCS=()
while IFS= read -r doc; do DOCS[${#DOCS[@]}]="$doc"; done < <(find "${PROJECT_ROOT}" \( -name .git -o -name .venv -o -name docs -o -name backup -o -name history \) -prune -o -type f \( -name "*.adoc" -o -name "*.asciidoc" \) -print | sort)

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

PLANTUML_FILES=()
while IFS= read -r source; do PLANTUML_FILES[${#PLANTUML_FILES[@]}]="$source"; done < <(find "${PROJECT_ROOT}" \( -name .git -o -name target -o -name .venv \) -prune -o -type f -name "*.plantuml" -print | sort)
echo
echo "=== PlantUML Shared Style Test ==="
PLANTUML_STYLE="${PROJECT_ROOT}/documentation/images/plantuml-style.puml"
if grep -Fqx 'footer (C)2026 Sean Donnellan' "${PLANTUML_STYLE}"; then
  echo "[ PASS ] Shared PlantUML footer"
else
  echo "[ FAIL ] Shared PlantUML footer missing"
  FAILED=$((FAILED + 1))
fi
for source in "${PLANTUML_FILES[@]}"; do
  rel_path="${source#${PROJECT_ROOT}/}"
  if grep -Eq '^!include .*plantuml-style\.puml$' "${source}"; then
    echo "[ PASS ] ${rel_path}"
  else
    echo "[ FAIL ] ${rel_path} (missing shared style include)"
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

      if grep -q 'Wainlux Pi Personal Non-Commercial and No-AI-Training License' "${TEMP_DIR}/README.html" && grep -q 'id="project-license"' "${TEMP_DIR}/README.html"; then
        echo "[ PASS ] Single project license is embedded"
      else
        echo "[ FAIL ] Project license text or anchor is missing from the single-page HTML"
        FAILED=$((FAILED + 1))
      fi

      if grep -q '<meta name="robots" content="noindex, nofollow, noarchive">' "${TEMP_DIR}/README.html" && grep -Fqx 'User-agent: *' "${PROJECT_ROOT}/robots.txt" && grep -Fqx 'Disallow: /' "${PROJECT_ROOT}/robots.txt"; then
        echo "[ PASS ] Advisory crawler requests are present"
      else
        echo "[ FAIL ] HTML robots metadata or root robots.txt is missing"
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
