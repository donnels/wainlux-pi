#!/usr/bin/env python3
"""
Generate A4 test grid sheets for laser alignment and movement testing.
8x8cm grids with 0.5cm squares, fitted to A4 with 2cm spacing.
"""

import argparse
import os

# A4 dimensions in mm
A4_WIDTH = 210
A4_HEIGHT = 297

# Grid parameters in mm
GRID_SIZE = 80  # 8cm
SQUARE_SIZE = 5  # 0.5cm
MARGIN = 20  # 2cm
GRID_SPACING = 5  # 0.5cm between grids


def generate_svg_grid():
    """Generate SVG with grids fitted to A4."""
    # Calculate how many grids fit
    usable_width = A4_WIDTH - 2 * MARGIN
    usable_height = A4_HEIGHT - 2 * MARGIN

    # Grids per row/column
    cols = int((usable_width + GRID_SPACING) / (GRID_SIZE + GRID_SPACING))
    rows = int((usable_height + GRID_SPACING) / (GRID_SIZE + GRID_SPACING))

    # Center the grid array
    total_width = cols * GRID_SIZE + (cols - 1) * GRID_SPACING
    total_height = rows * GRID_SIZE + (rows - 1) * GRID_SPACING
    start_x = (A4_WIDTH - total_width) / 2
    start_y = (A4_HEIGHT - total_height) / 2

    svg = ['<?xml version="1.0" encoding="UTF-8"?>']
    svg.append(f'<svg width="{A4_WIDTH}mm" height="{A4_HEIGHT}mm" ')
    svg.append(f'viewBox="0 0 {A4_WIDTH} {A4_HEIGHT}" ')
    svg.append('xmlns="http://www.w3.org/2000/svg">')
    svg.append('<rect width="100%" height="100%" fill="white"/>')

    for row in range(rows):
        for col in range(cols):
            x = start_x + col * (GRID_SIZE + GRID_SPACING)
            y = start_y + row * (GRID_SIZE + GRID_SPACING)

            # Grid outline
            svg.append(
                f'<rect x="{x}" y="{y}" width="{GRID_SIZE}" height="{GRID_SIZE}" '
            )
            svg.append('fill="none" stroke="black" stroke-width="0.5"/>')

            # Grid squares
            for i in range(int(GRID_SIZE / SQUARE_SIZE)):
                for j in range(int(GRID_SIZE / SQUARE_SIZE)):
                    gx = x + i * SQUARE_SIZE
                    gy = y + j * SQUARE_SIZE
                    svg.append(
                        f'<rect x="{gx}" y="{gy}" width="{SQUARE_SIZE}" height="{SQUARE_SIZE}" '
                    )
                    svg.append('fill="none" stroke="gray" stroke-width="0.2"/>')

            # Grid labels (size in cm at corners)
            svg.append(
                f'<text x="{x+2}" y="{y+4}" '
                f'font-family="monospace" font-size="3" fill="black">8x8cm</text>'
            )

    # Page info
    svg.append(
        f'<text x="{A4_WIDTH/2}" y="{A4_HEIGHT-5}" font-family="monospace" font-size="4" '
    )
    svg.append(
        f'text-anchor="middle" fill="black">{cols}x{rows} grids | 0.5cm squares | A4</text>'
    )

    svg.append("</svg>")

    return "\n".join(svg)


def main():
    parser = argparse.ArgumentParser(description="Generate A4 test grid sheets")
    parser.add_argument(
        "-o",
        "--output",
        default="images/test_grid.pdf",
        help="Output PDF file (default: images/test_grid.pdf)",
    )
    parser.add_argument(
        "-n",
        "--pages",
        type=int,
        default=1,
        help="Number of pages to generate (default: 1)",
    )
    parser.add_argument(
        "--svg-only", action="store_true", help="Generate SVG only (no PDF conversion)"
    )
    args = parser.parse_args()

    # Ensure output directory exists
    output_dir = os.path.dirname(args.output) or "."
    os.makedirs(output_dir, exist_ok=True)

    if args.pages == 1:
        svg_content = generate_svg_grid()
        svg_file = args.output.replace(".pdf", ".svg")

        with open(svg_file, "w") as f:
            f.write(svg_content)
        print(f"Generated {svg_file}")

        if not args.svg_only:
            try:
                import cairosvg

                cairosvg.svg2pdf(
                    bytestring=svg_content.encode("utf-8"), write_to=args.output
                )
                print(f"Generated {args.output}")
            except ImportError:
                print("Warning: cairosvg not installed. Run: pip install cairosvg")
                print(f"SVG only: {svg_file}")

        print("Grid: 8x8cm with 0.5cm squares")
        print("Print on A4 at 100% scale")
    else:
        for i in range(args.pages):
            svg_content = generate_svg_grid()
            svg_file = args.output.replace(".pdf", f"_{i+1}.svg")
            pdf_file = args.output.replace(".pdf", f"_{i+1}.pdf")

            with open(svg_file, "w") as f:
                f.write(svg_content)

            if not args.svg_only:
                try:
                    import cairosvg

                    cairosvg.svg2pdf(
                        bytestring=svg_content.encode("utf-8"), write_to=pdf_file
                    )
                    print(f"Generated {pdf_file}")
                except ImportError:
                    print(f"Generated {svg_file} (cairosvg not installed)")
            else:
                print(f"Generated {svg_file}")

        print(f"\nTotal {args.pages} pages")
        print("Grid: 8x8cm with 0.5cm squares")
        print("Print on A4 at 100% scale")


if __name__ == "__main__":
    main()
