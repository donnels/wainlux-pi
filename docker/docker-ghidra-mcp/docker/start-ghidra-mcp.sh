#!/bin/sh
set -eu

: "${GHIDRA_INPUT_JAR:=/input/Laser.jar}"
: "${GHIDRA_PROJECT_PATH:=/project}"
: "${GHIDRA_PROJECT_NAME:=wainlux-k6}"

if [ ! -r "$GHIDRA_INPUT_JAR" ]; then
  echo "Ghidra input JAR is not readable: $GHIDRA_INPUT_JAR" >&2
  exit 2
fi

mkdir -p /tmp/laser-classes "$GHIDRA_PROJECT_PATH"
cd /tmp/laser-classes
jar xf "$GHIDRA_INPUT_JAR"

set -- $(find /tmp/laser-classes -type f -name '*.class')
if [ "$#" -eq 0 ]; then
  echo "No class files found in $GHIDRA_INPUT_JAR" >&2
  exit 2
fi

exec pyghidra-mcp \
  --transport streamable-http \
  --host 0.0.0.0 \
  --port 8000 \
  --project-path "$GHIDRA_PROJECT_PATH" \
  --project-name "$GHIDRA_PROJECT_NAME" \
  --wait-for-analysis \
  "$@"
