#!/usr/bin/env bash
# Clean up venv and temporary files
set -e
VENV_DIR="scripts/venv"
if [[ -d "$VENV_DIR" ]]; then
  echo "Removing virtualenv: $VENV_DIR"
  rm -rf "$VENV_DIR"
  echo "Removed."
else
  echo "No virtualenv found at $VENV_DIR"
fi

echo "Done. To rebuild run: bash scripts/setup_venv.sh"