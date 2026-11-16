#!/bin/bash
# Generate protocol and PlantUML diagrams, and update image catalog index

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

# Render PlantUML diagrams if container engine is available
PLANTUML_IMAGE=""
for candidate in "docker.io/plantuml/plantuml:1.2025.4" "docker.io/plantuml/plantuml:latest"; do
    if container_host image inspect "$candidate" >/dev/null 2>&1; then
        PLANTUML_IMAGE="$candidate"
        break
    fi
done

if [ -n "$PLANTUML_IMAGE" ]; then
    echo "Generating diagrams from PlantUML specs using $PLANTUML_IMAGE..."
    for puml_file in "$IMAGES_DIR"/*.plantuml; do
        if [ -f "$puml_file" ]; then
            base_name=$(basename "$puml_file")
            echo "  $base_name → PNG"
            container_host run --rm \
                -v "$IMAGES_DIR:/repo" \
                -w /repo \
                "$PLANTUML_IMAGE" \
                "/repo/${base_name}"
        fi
    done
fi

# Rebuild image catalog index
echo "Generating image catalog index..."
"$SCRIPT_DIR/generate_image_index.sh"

echo "Done."
