#!/usr/bin/env bash

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

ssh_host() { host_command ssh "$@"; }