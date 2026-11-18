#!/usr/bin/env bash

CONTAINER_LIB_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
CONTAINER_REPO_ROOT="$(cd "$CONTAINER_LIB_DIR/../.." && pwd)"
CONTAINER_IMAGE_MAP="$CONTAINER_REPO_ROOT/docker/IMAGES.map"

host_command() {
  if [[ -n "${FLATPAK_ID:-}" || -f /.flatpak-info ]]; then
    if command -v flatpak-spawn >/dev/null 2>&1; then
      flatpak-spawn --host "$@"
    elif command -v host-spawn >/dev/null 2>&1; then
      host-spawn "$@"
    else
      echo "Flatpak detected, but neither host-spawn nor flatpak-spawn is available." >&2
      return 127
    fi
  elif command -v "$1" >/dev/null 2>&1; then
    "$@"
  else
    echo "Required host command not found: $1" >&2
    return 127
  fi
}

container_engine() {
  if [[ -n "${CONTAINER_ENGINE_SELECTED:-}" ]]; then
    return 0
  fi

  local candidate
  if [[ -n "${CONTAINER_ENGINE:-}" ]]; then
    case "$CONTAINER_ENGINE" in
      docker|podman) candidate="$CONTAINER_ENGINE" ;;
      *) echo "CONTAINER_ENGINE must be docker or podman." >&2; return 2 ;;
    esac
    if ! host_command "$candidate" info >/dev/null 2>&1; then
      echo "CONTAINER_ENGINE=$candidate is not available or its engine is not running." >&2
      return 1
    fi
    CONTAINER_ENGINE_SELECTED="$candidate"
    return 0
  fi

  for candidate in docker podman; do
    if host_command "$candidate" info >/dev/null 2>&1; then
      CONTAINER_ENGINE_SELECTED="$candidate"
      return 0
    fi
  done

  echo "No running Docker or Podman engine is available on the host." >&2
  return 127
}

container_host() {
  container_engine || return
  host_command "$CONTAINER_ENGINE_SELECTED" "$@"
}

container_image_settings() {
  local wanted="$1" name
  CONTAINER_IMAGE_REF=""
  CONTAINER_IMAGE_CONTEXT=""
  CONTAINER_IMAGE_PLATFORM=""
  CONTAINER_IMAGE_IGNORE=""
  CONTAINER_IMAGE_POLICY="scan"

  while IFS='|' read -r name CONTAINER_IMAGE_REF CONTAINER_IMAGE_CONTEXT CONTAINER_IMAGE_PLATFORM CONTAINER_IMAGE_IGNORE CONTAINER_IMAGE_POLICY; do
    [[ -z "$name" || "$name" == \#* ]] && continue
    if [[ "$name" == "$wanted" ]]; then
      return 0
    fi
  done < "$CONTAINER_IMAGE_MAP"

  echo "Unknown container image '$wanted' in $CONTAINER_IMAGE_MAP." >&2
  return 2
}

container_image_ref() {
  container_image_settings "$1" || return
  printf '%s\n' "$CONTAINER_IMAGE_REF"
}

container_require_image() {
  local name="$1" image
  image="$(container_image_ref "$name")" || return
  if ! container_host image inspect "$image" >/dev/null 2>&1; then
    echo "Selected local image $image is not present. Build it with ./scripts/build_local_container.sh $name, or use the explicit scan and promotion path in ./scripts/update_container_image.sh." >&2
    return 1
  fi
  printf '%s\n' "$image"
}

container_build_local() {
  local name="$1" context
  local -a build_args=()
  container_image_settings "$name" || return
  container_engine || return
  context="$CONTAINER_REPO_ROOT/$CONTAINER_IMAGE_CONTEXT"
  if [[ ! -f "$context/Dockerfile" ]]; then
    echo "Dockerfile not found for '$name': $context/Dockerfile" >&2
    return 2
  fi
  if [[ -n "$CONTAINER_IMAGE_PLATFORM" && "$CONTAINER_IMAGE_PLATFORM" != "-" ]]; then
    build_args+=(--platform "$CONTAINER_IMAGE_PLATFORM")
  fi
  echo "Building $name as selected local image $CONTAINER_IMAGE_REF (no scan or promotion)."
  container_host build "${build_args[@]}" --tag "$CONTAINER_IMAGE_REF" "$context"
}

container_record_scan_evidence() {
  local context="$1" readme="$1/README.adoc" marker='<!-- container-scan-evidence -->'
  if [[ ! -f "$readme" ]]; then
    echo "Image README not found: $readme" >&2
    return 1
  fi
  if grep -Fq "$marker" "$readme"; then
    return 0
  fi
  cat >> "$readme" <<'ASCIIDOC'

ifndef::single-page[]
== Build and Scan Evidence

This is the latest compact Trivy summary. Manual scan commands and CSV filters are documented in `docker/docker-trivy/README.adoc`. Any accepted findings, use limits, and expiry are stated in this image README; see ADR-029 for the exception policy.

.Latest Trivy summary
[source,csv]
----
include::trivy-report.csv[]
----

.Image Dockerfile
[source,dockerfile]
----
include::Dockerfile[]
----

<!-- container-scan-evidence -->
endif::single-page[]
ASCIIDOC
}

container_update_image() {
  local name="$1" force_scan="${2:-}" allow_findings="${3:-}" candidate repo image_id version_ref old_id retained_ref context version_id report_file scan_status=0
  local -a build_args=()
  container_image_settings "$name" || return
  container_engine || return

  context="$CONTAINER_REPO_ROOT/$CONTAINER_IMAGE_CONTEXT"
  if [[ ! -f "$context/Dockerfile" ]]; then
    echo "Dockerfile not found for '$name': $context/Dockerfile" >&2
    return 2
  fi
  repo="${CONTAINER_IMAGE_REF%:*}"
  candidate="$repo:candidate-$(date -u +%Y%m%dT%H%M%SZ)-$$-$RANDOM"
  if [[ -n "$CONTAINER_IMAGE_PLATFORM" && "$CONTAINER_IMAGE_PLATFORM" != "-" ]]; then
    build_args+=(--platform "$CONTAINER_IMAGE_PLATFORM")
  fi

  echo "Building candidate $candidate from pinned inputs..."
  container_host build "${build_args[@]}" --tag "$candidate" "$context" || return
  image_id="$(container_host image inspect "$candidate" --format '{{.Id}}')" || return
  version_ref="$repo:sha-${image_id#sha256:}"

  if [[ "$force_scan" != "--rescan" ]] \
    && old_id="$(container_host image inspect "$CONTAINER_IMAGE_REF" --format '{{.Id}}' 2>/dev/null)" \
    && [[ "$old_id" == "$image_id" ]] \
    && version_id="$(container_host image inspect "$version_ref" --format '{{.Id}}' 2>/dev/null)" \
    && [[ "$version_id" == "$image_id" ]]; then
    echo "Image ID $image_id is already selected and has a promoted version tag; skipping the repeat scan."
    container_host rmi "$candidate" >/dev/null 2>&1 || true
    return 0
  fi

  if [[ "$CONTAINER_IMAGE_POLICY" == "trust" ]]; then
    echo "Skipping self-scan for scanner trust-root candidate $image_id."
  else
    echo "Scanning candidate image ID $image_id..."
    report_file="$context/trivy-report.csv"
    if [[ -n "$CONTAINER_IMAGE_IGNORE" && "$CONTAINER_IMAGE_IGNORE" != "-" ]]; then
      container_scan_image "$image_id" "$CONTAINER_REPO_ROOT/$CONTAINER_IMAGE_IGNORE" "$report_file" || scan_status=$?
    else
      container_scan_image "$image_id" "" "$report_file" || scan_status=$?
    fi
    container_record_scan_evidence "$context" || return
    if (( scan_status != 0 )); then
      if [[ "$allow_findings" == "--allow-findings" ]] \
        && grep -Fq 'record,metric,value' "$report_file" \
        && (( scan_status == 1 )); then
        echo "Temporarily accepting reported findings for $name; see $report_file and $context/README.adoc." >&2
      else
        echo "Candidate $candidate did not pass the scan. The selected image $CONTAINER_IMAGE_REF is unchanged." >&2
        return "$scan_status"
      fi
    fi
  fi

  if container_host image inspect "$version_ref" >/dev/null 2>&1; then
    local tagged_id
    tagged_id="$(container_host image inspect "$version_ref" --format '{{.Id}}')"
    if [[ "$tagged_id" != "$image_id" ]]; then
      echo "Version tag collision: $version_ref points to $tagged_id, expected $image_id." >&2
      return 1
    fi
  else
    container_host tag "$image_id" "$version_ref" || return
  fi

  if old_id="$(container_host image inspect "$CONTAINER_IMAGE_REF" --format '{{.Id}}' 2>/dev/null)"; then
    if [[ "$old_id" != "$image_id" ]]; then
      retained_ref="$repo:retained-${old_id#sha256:}"
      container_host tag "$old_id" "$retained_ref" || return
      echo "Retained previous image as $retained_ref."
    fi
  fi

  container_host tag "$image_id" "$CONTAINER_IMAGE_REF" || return
  container_host rmi "$candidate" >/dev/null 2>&1 || true
  echo "Promoted $version_ref as $CONTAINER_IMAGE_REF."
}

container_restore_image() {
  local name="$1" version_ref="$2" image repo image_id old_id retained_ref
  image="$(container_image_ref "$name")" || return
  repo="${image%:*}"
  case "$version_ref" in
    "$repo":sha-*|"$repo":retained-*) ;;
    *) echo "Rollback image must be a sha- or retained- tag in $repo." >&2; return 2 ;;
  esac
  image_id="$(container_host image inspect "$version_ref" --format '{{.Id}}')" || {
    echo "Rollback image not found: $version_ref" >&2
    return 1
  }

  if old_id="$(container_host image inspect "$image" --format '{{.Id}}' 2>/dev/null)" && [[ "$old_id" != "$image_id" ]]; then
    retained_ref="$repo:retained-${old_id#sha256:}"
    container_host tag "$old_id" "$retained_ref" || return
    echo "Retained replaced image as $retained_ref."
  fi
  container_host tag "$image_id" "$image"
  echo "Selected $version_ref as $image."
}

container_scan_image() {
  local image="$1"
  local ignore_file="${2:-}"
  local report_file="${3:-}"
  local scanner_image
  local scanner_dir
  local scan_json
  local scan_errors
  local scan_output
  local scan_status=0
  local image_id
  local -a ignore_args=()

  scanner_image="$(container_image_ref trivy)" || return
  if ! container_host image inspect "$scanner_image" >/dev/null 2>&1; then
    echo "Building and promoting pinned scanner trust root..."
    container_update_image trivy || return
  fi

  if ! host_command jq --version >/dev/null 2>&1; then
    echo "jq is required on the host to turn Trivy JSON into compact CSV." >&2
    return 127
  fi

  if [[ -n "$ignore_file" ]]; then
    if [[ ! -f "$ignore_file" ]]; then
      echo "Trivy exception file not found: $ignore_file" >&2
      return 2
    fi
    ignore_args+=(--ignorefile /ignore/.trivyignore --show-suppressed)
  fi
  scanner_dir="$(mktemp -d "$CONTAINER_REPO_ROOT/.container-scan-tmp.XXXXXX")"
  image_id="$(container_host image inspect "$image" --format '{{.Id}}')" || {
    rm -rf "$scanner_dir"
    return 1
  }
  echo "Scanning image ID $image_id (HIGH/CRITICAL; compact CSV summary)..."
  if ! container_host save --output "$scanner_dir/image.tar" "$image_id"; then
    rm -rf "$scanner_dir"
    return 1
  fi
  local -a scan_mounts=(-v "$scanner_dir:/scan:ro")
  scan_json="$scanner_dir/trivy-report.json"
  scan_errors="$scanner_dir/trivy-errors.txt"
  scan_output="$scanner_dir/trivy-report.csv"
  if [[ -n "$ignore_file" ]]; then
    scan_mounts+=(-v "$ignore_file:/ignore/.trivyignore:ro")
  fi
  container_host run --rm \
    "${scan_mounts[@]}" \
    -v wainlux-trivy-cache:/root/.cache \
    "$scanner_image" image --input /scan/image.tar \
    --severity HIGH,CRITICAL --exit-code 1 --no-progress --quiet --format json "${ignore_args[@]}" >"$scan_json" 2>"$scan_errors" || scan_status=$?
  if ! host_command jq -e '.Results | type == "array"' "$scan_json" >/dev/null 2>&1; then
    echo "Trivy did not produce a complete JSON report (exit $scan_status)." >&2
    tail -n 20 "$scan_errors" >&2
    rm -rf "$scanner_dir"
    return 2
  fi
  if grep -Eiq '(^|[[:space:]])(FATAL|ERROR)([[:space:]:]|$)' "$scan_errors"; then
    echo "Trivy reported an operational error; this scan cannot be overridden." >&2
    tail -n 20 "$scan_errors" >&2
    rm -rf "$scanner_dir"
    return 2
  fi
  local scanned_utc
  scanned_utc="$(date -u +%Y-%m-%dT%H:%M:%SZ)"
  if ! host_command jq -r --arg image_id "$image_id" --arg scanned_utc "$scanned_utc" \
      -f "$CONTAINER_REPO_ROOT/scripts/trivy-summary.jq" "$scan_json" > "$scan_output"; then
    echo "Could not convert the Trivy JSON report to the compact CSV summary." >&2
    rm -rf "$scanner_dir"
    return 2
  fi
  if [[ -n "$report_file" ]]; then
    cat "$scan_output" > "$report_file"
  fi
  cat "$scan_output"
  rm -rf "$scanner_dir"
  return "$scan_status"
}

ssh_host() { host_command ssh "$@"; }
