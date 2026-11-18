#!/usr/bin/env python3
"""
Add gradient background to bytefield-svg diagrams.
Inserts a gradient from top-left (#fff) to bottom-right (#aaf).
"""

import sys
import xml.etree.ElementTree as ET
from pathlib import Path


def add_gradient_background(svg_path: Path) -> None:
    """Add gradient background to SVG file in-place."""

    # Register SVG namespace to preserve xmlns
    ET.register_namespace("", "http://www.w3.org/2000/svg")

    tree = ET.parse(svg_path)
    root = tree.getroot()
    root.set("data-copyright", "Copyright (c) 2026 Sean Donnellan")
    if not any(child.tag is ET.Comment for child in root):
        root.insert(0, ET.Comment("Copyright (c) 2026 Sean Donnellan"))

    # Get viewBox dimensions
    viewbox = root.get("viewBox", "0 0 672 200")
    x, y, width, height = map(float, viewbox.split())

    # Create defs element if not exists
    defs = root.find("{http://www.w3.org/2000/svg}defs")
    if defs is None:
        defs = ET.Element("{http://www.w3.org/2000/svg}defs")
        root.insert(0, defs)

    # Keep reruns from duplicating the gradient, background or attribution.
    gradient = defs.find("{http://www.w3.org/2000/svg}linearGradient[@id='bg-gradient']")
    if gradient is None:
        gradient = ET.SubElement(defs, "{http://www.w3.org/2000/svg}linearGradient")
        gradient.set("id", "bg-gradient")
        gradient.set("x1", "0%")
        gradient.set("y1", "0%")
        gradient.set("x2", "100%")
        gradient.set("y2", "100%")

        stop1 = ET.SubElement(gradient, "{http://www.w3.org/2000/svg}stop")
        stop1.set("offset", "0%")
        stop1.set("style", "stop-color:#ffffff;stop-opacity:1")

        stop2 = ET.SubElement(gradient, "{http://www.w3.org/2000/svg}stop")
        stop2.set("offset", "100%")
        stop2.set("style", "stop-color:#aaaaff;stop-opacity:1")

    if not any(child.tag is ET.Comment for child in root):
        root.insert(0, ET.Comment("Copyright (c) 2026 Sean Donnellan"))
    for child in list(root):
        if child.tag == "{http://www.w3.org/2000/svg}rect" and child.get("fill") == "url(#bg-gradient)":
            root.remove(child)

    # Create background rectangle
    bg_rect = ET.Element("{http://www.w3.org/2000/svg}rect")
    bg_rect.set("x", str(x))
    bg_rect.set("y", str(y))
    bg_rect.set("width", str(width))
    bg_rect.set("height", str(height))
    bg_rect.set("fill", "url(#bg-gradient)")

    # Insert after defs as first visible element
    insert_pos = list(root).index(defs) + 1 if defs in root else 0
    root.insert(insert_pos, bg_rect)

    # Write back
    tree.write(svg_path, encoding="utf-8", xml_declaration=True)
    print(f"✓ Added gradient background to {svg_path.name}")


def main():
    if len(sys.argv) < 2:
        print("Usage: add_svg_background.py <svg_file> [<svg_file> ...]")
        sys.exit(1)

    for path_str in sys.argv[1:]:
        path = Path(path_str)
        if not path.exists():
            print(f"✗ Not found: {path}", file=sys.stderr)
            continue
        if path.suffix.lower() != ".svg":
            print(f"✗ Not SVG: {path}", file=sys.stderr)
            continue

        try:
            add_gradient_background(path)
        except Exception as e:
            print(f"✗ Failed {path.name}: {e}", file=sys.stderr)


if __name__ == "__main__":
    main()
