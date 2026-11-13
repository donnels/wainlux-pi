#!/bin/bash
# Generate protocol diagrams using bytefield-svg container

set -e

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
IMAGES_DIR="$PROJECT_ROOT/documentation/images"
source "$SCRIPT_DIR/lib/container.sh"

# Build container if not exists
if ! container_host image inspect bytefield-svg >/dev/null 2>&1; then
    echo "Building bytefield-svg container..."
    container_host build -t bytefield-svg "$PROJECT_ROOT/docker/docker-bytefield"
fi

# Generate diagrams from EDN specs in documentation/images/
echo "Generating diagrams from EDN specs..."

for edn_file in "$IMAGES_DIR"/*.edn; do
    if [ -f "$edn_file" ]; then
        base_name=$(basename "$edn_file" .edn)
        svg_file="$IMAGES_DIR/${base_name}.svg"
        
        echo "  $base_name.edn → $base_name.svg"
        
        container_host run --rm \
            -v "$IMAGES_DIR:/diagrams" \
            bytefield-svg \
            "/diagrams/${base_name}.edn" \
            -o "/diagrams/${base_name}.svg"
    fi
done

echo "Done."
