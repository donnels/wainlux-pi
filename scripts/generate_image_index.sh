#!/usr/bin/env bash
# Create documentation/images/README.adoc from image filenames and PlantUML titles.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
IMAGES_DIR="$PROJECT_ROOT/documentation/images"
OUTPUT="$IMAGES_DIR/README.adoc"
TEMP_OUTPUT="$(mktemp "$IMAGES_DIR/.README.adoc.XXXXXX")"
trap 'rm -f "$TEMP_OUTPUT"' EXIT

# Prefixes match the naming convention in README-include.adoc.
image_category() {
    case "$1" in
        arch) printf 'Architecture\n' ;;
        app)  printf 'Application\n' ;;
        biz)  printf 'Business\n' ;;
        test) printf 'Testing\n' ;;
        prot) printf 'Protocol\n' ;;
        hw)   printf 'Hardware\n' ;;
        capt) printf 'Capture\n' ;;
        doc)  printf 'Documentation\n' ;;
        *)    printf 'Other\n' ;;
    esac
}

image_title() {
    local base="$2" source title

    for source in "$IMAGES_DIR/$base.plantuml" "$IMAGES_DIR/$base.puml"; do
        [[ -f "$source" ]] || continue
        title="$(sed -nE 's/^[[:space:]]*title[[:space:]]+//Ip' "$source" | head -n 1)"
        if [[ -n "$title" ]]; then
            printf '%s\n' "$title"
            return
        fi
    done

    # Turn a filename stem into a readable fallback.
    base="${base//-/ }"
    printf '%s\n' "$base" | sed -E 's/^([[:alpha:]])/\U\1/'
}

write_header() {
    cat <<'ASCIIDOC'
= Images Catalog
:toc:
:toclevels: 5
:sectnums:
:sectnumlevels: 5

include::README-include.adoc[leveloffset=+1]
ASCIIDOC
}

write_category() {
    local category="$1" filename base prefix title anchor found=0

    while IFS= read -r filename; do
        [[ -n "$filename" ]] || continue
        base="${filename%.*}"
        prefix="${base%%-*}"
        [[ "$base" == README* || "$base" == style-* ]] && continue
        [[ "$(image_category "$prefix")" == "$category" ]] || continue

        if (( found == 0 )); then
            printf '== %s\n\n' "$category"
            found=1
        fi

        title="$(image_title "$filename" "$base")"
        anchor="image-${base//[^[:alnum:]_-]/-}"
        printf '[[%s]]\n=== %s\n\n.%s\nimage::%s[%s,align=center,width=80%%]\n' \
            "$anchor" "$title" "$title" "$filename" "$title"
    done < <(find "$IMAGES_DIR" -maxdepth 1 -type f \
        \( -iname '*.png' -o -iname '*.svg' -o -iname '*.jpg' \
        -o -iname '*.jpeg' -o -iname '*.gif' \) \
        -printf '%f\n' | LC_ALL=C sort)
}

write_index() {
    local category
    write_header
    for category in Architecture Application Business Testing Protocol Hardware Capture Documentation Other; do
        write_category "$category"
    done
}

write_index > "$TEMP_OUTPUT"
mv "$TEMP_OUTPUT" "$OUTPUT"
trap - EXIT
printf 'Image catalog index written to %s\n' "$OUTPUT"
