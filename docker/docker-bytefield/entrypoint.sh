#!/bin/sh
# Wrapper for bytefield-svg that adds gradient background

set -e

# Run bytefield-svg with all arguments
bytefield-svg "$@"

# Extract output file from arguments (-o flag)
output_file=""
while [ $# -gt 0 ]; do
    if [ "$1" = "-o" ] && [ -n "$2" ]; then
        output_file="$2"
        break
    fi
    shift
done

# If output file was specified and exists, add gradient background
if [ -n "$output_file" ] && [ -f "$output_file" ]; then
    /usr/local/bin/add_svg_background.py "$output_file"
fi
