#!/bin/bash
# Generate protocol diagrams using bytefield-svg container

set -e

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
IMAGES_DIR="$PROJECT_ROOT/images"

# Use host-spawn if available (Flatpak), otherwise direct podman
if command -v host-spawn >/dev/null 2>&1; then
    PODMAN="host-spawn podman"
else
    PODMAN="podman"
fi

# Build container if not exists
if ! $PODMAN image inspect bytefield-svg >/dev/null 2>&1; then
    echo "Building bytefield-svg container..."
    $PODMAN build -t bytefield-svg "$PROJECT_ROOT/docker-bytefield"
fi

# Generate diagrams from EDN specs in images/
echo "Generating diagrams from EDN specs..."

for edn_file in "$IMAGES_DIR"/*.edn; do
    if [ -f "$edn_file" ]; then
        base_name=$(basename "$edn_file" .edn)
        svg_file="$IMAGES_DIR/${base_name}.svg"
        
        echo "  $base_name.edn → $base_name.svg"
        
        $PODMAN run --rm \
            -v "$IMAGES_DIR:/diagrams:Z" \
            bytefield-svg \
            "/diagrams/${base_name}.edn" \
            -o "/diagrams/${base_name}.svg"
    fi
done

echo "Done."
