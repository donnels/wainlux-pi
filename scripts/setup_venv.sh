#!/bin/bash
# Setup Python virtual environment for K6 scripts

set -e

VENV_DIR="venv"

# Work relative to this script
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REQUIREMENTS="$SCRIPT_DIR/requirements.txt"

echo "Checking prerequisites..."

# Check if python3-venv is available
if ! python3 -m venv --help &>/dev/null; then
    echo "python3-venv not found. Installing..."
    sudo apt update
    sudo apt install -y python3-venv python3-pip
fi

echo "Creating virtual environment in $SCRIPT_DIR/$VENV_DIR..."
python3 -m venv "$SCRIPT_DIR/$VENV_DIR"

echo "Activating virtual environment..."
# shellcheck disable=SC1090
source "$SCRIPT_DIR/$VENV_DIR/bin/activate"

echo "Upgrading pip..."
python -m pip install --upgrade pip

if [[ -f "$REQUIREMENTS" ]]; then
    echo "Installing requirements from $REQUIREMENTS..."
    python -m pip install -r "$REQUIREMENTS"
else
    echo "No requirements file found at $REQUIREMENTS, skipping package install."
fi
echo ""
echo "Setup complete!"
echo ""
echo "To activate the virtual environment, run:"
echo "  source venv/bin/activate"
echo ""
echo "Then run scripts normally:"
echo "  ./burn_k6_image.py --help"
echo "  ./statistics.py --help"
