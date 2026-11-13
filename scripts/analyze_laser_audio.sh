#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
IMAGE="localhost/wainlux-audio-analysis:local"

source "$SCRIPT_DIR/lib/container.sh"

if [[ "${1:-}" == "--help" || $# -lt 1 ]]; then
  printf 'Usage: %s RECORDING.m4a [--run-log API_LOG.json] [--attempt-id ATTEMPT-ID] [--expected-id DDDDDD]\n\nAnalyze audio in a container and write RECORDING.analysis.html beside it. Audio and logs are mounted read-only.\n' "${BASH_SOURCE[0]##*/}"
  if [[ "${1:-}" == "--help" ]]; then exit 0; else exit 2; fi
fi

RECORDING="$1"
shift
if [[ ! -f "$RECORDING" ]]; then
  echo "Recording not found: $RECORDING" >&2
  exit 2
fi
RECORDING_DIR="$(cd "$(dirname "$RECORDING")" && pwd)"
RECORDING_NAME="$(basename "$RECORDING")"
RUN_LOG=""
ANALYZER_ARGS=()
while [[ $# -gt 0 ]]; do
  case "$1" in
    --run-log)
      if [[ $# -lt 2 || ! -f "$2" ]]; then echo "--run-log requires an existing API log file." >&2; exit 2; fi
      RUN_LOG="$(cd "$(dirname "$2")" && pwd)/$(basename "$2")"
      shift 2
      ;;
    *) ANALYZER_ARGS+=("$1"); shift ;;
  esac
done

container_engine
if ! container_host image inspect "$IMAGE" >/dev/null 2>&1; then
  container_host build -t "$IMAGE" "$SCRIPT_DIR/../docker/docker-audio-analysis"
fi

CONTAINER_ARGS=(run --rm
  --mount "type=bind,source=$RECORDING_DIR,target=/recording,readonly"
  --mount "type=bind,source=$RECORDING_DIR,target=/report"
)
if [[ -n "$RUN_LOG" ]]; then
  CONTAINER_ARGS+=(--mount "type=bind,source=$RUN_LOG,target=/run-log.json,readonly")
fi
CONTAINER_ARGS+=("$IMAGE" "/recording/$RECORDING_NAME" --html-report "/report/${RECORDING_NAME%.*}.analysis.html")
if [[ -n "$RUN_LOG" ]]; then CONTAINER_ARGS+=(--run-log /run-log.json); fi
CONTAINER_ARGS+=("${ANALYZER_ARGS[@]}")
container_host "${CONTAINER_ARGS[@]}"