#!/bin/bash
# Generate protocol and PlantUML diagrams, and update image catalog index

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
IMAGES_DIR="$PROJECT_ROOT/documentation/images"
source "$SCRIPT_DIR/lib/container.sh"

BYTEFIELD_IMAGE=""
PLANTUML_IMAGE=""

container_engine
BYTEFIELD_IMAGE="$(container_require_image bytefield)"
PLANTUML_IMAGE="$(container_require_image plantuml)"

# Generate diagrams from EDN specs in documentation/images/
echo "Generating diagrams from EDN specs..."

for edn_file in "$IMAGES_DIR"/*.edn; do
    if [ -f "$edn_file" ]; then
        base_name=$(basename "$edn_file" .edn)
        echo "  $base_name.edn → $base_name.svg"
        
        container_host run --rm \
            -v "$IMAGES_DIR:/diagrams" \
            "$BYTEFIELD_IMAGE" \
            "/diagrams/${base_name}.edn" \
            -o "/diagrams/${base_name}.svg"
    fi
done

echo "Generating diagrams from PlantUML specs using $PLANTUML_IMAGE..."
PLANTUML_USERNS_ARGS=()
if [[ "$CONTAINER_ENGINE_SELECTED" == "podman" ]]; then
    # Preserve the image's non-root UID while allowing writes to Deck-owned files.
    PLANTUML_USERNS_ARGS+=(--userns=keep-id)
fi

for puml_file in "$IMAGES_DIR"/*.plantuml; do
    if [ -f "$puml_file" ]; then
        base_name=$(basename "$puml_file")
        echo "  $base_name → SVG"
        container_host run --rm "${PLANTUML_USERNS_ARGS[@]}" \
            -v "$IMAGES_DIR:/diagrams" \
            -w /diagrams \
            "$PLANTUML_IMAGE" \
            -tsvg "$base_name"
    fi
done

echo "Generating PNG companions for every documentation SVG..."
for svg_file in "$IMAGES_DIR"/*.svg; do
    [[ -f "$svg_file" ]] || continue
    base_name="$(basename "$svg_file" .svg)"
    echo "  $base_name.svg → $base_name.png"
    container_host run --rm \
        -v "$IMAGES_DIR:/diagrams" \
        --entrypoint rsvg-convert \
        "$BYTEFIELD_IMAGE" \
        --output "/diagrams/$base_name.png" \
        "/diagrams/$base_name.svg"
done

# Rebuild image catalog index
echo "Generating image catalog index..."
"$SCRIPT_DIR/generate_image_index.sh"

echo "Done."
