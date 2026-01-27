#!/usr/bin/env python3
"""
Generate WiFi QR code image for laser engraving

Creates a QR code for WiFi credentials with visible text (SSID + description).
Password is encoded in QR but NOT displayed as text.

Output is sized for credit card bamboo blanks (85.6mm × 53.98mm).
Uses high error correction (30%) for laser engraving tolerance.

Usage:
  ./generate_qr_wifi.py -s MyNetwork -p SecretPass123 -d "Guest WiFi" -o wifi.png
"""

import argparse
import qrcode
from datetime import datetime
from PIL import Image, ImageDraw, ImageFont


def generate_wifi_qr(
    ssid, password, security="WPA", hidden=False, description=None, output=None
):
    """
    Generate WiFi QR code with text labels

    Args:
        ssid: WiFi network name
        password: WiFi password (in QR only, not displayed)
        security: WPA, WEP, or nopass
        hidden: Whether SSID is hidden
        description: Optional text description to display
        output: Output filename (default: QR-WIFI-YYMMDDHHMMSS.png)

    Returns:
        Path to generated image
    """
    # Generate default filename with timestamp if not provided
    if output is None:
        timestamp_str = datetime.now().strftime("%y%m%d%H%M%S")
        output = f"QR-WIFI-{timestamp_str}.png"

    # WiFi QR format: WIFI:T:<type>;S:<ssid>;P:<password>;H:<hidden>;;
    wifi_data = (
        f"WIFI:T:{security};S:{ssid};P:{password};H:{'true' if hidden else 'false'};;"
    )

    # Generate QR code with high error correction for laser engraving
    qr = qrcode.QRCode(
        version=None,  # Auto-size
        error_correction=qrcode.constants.ERROR_CORRECT_H,  # 30% error correction
        box_size=10,
        border=2,
    )
    qr.add_data(wifi_data)
    qr.make(fit=True)

    # Create QR image
    qr_img = qr.make_image(fill_color="black", back_color="white")

    # K6 max burn area: ~80mm width (conservative: 75mm)
    # Credit card height: 53.98mm fits within 80mm vertical limit
    # K6 native resolution: 0.05 mm/pixel = 20 pixels/mm
    # 75mm × 53.98mm = 1500 × 1080 pixels at K6 resolution
    # Card is left-aligned; right edge extends beyond burn area
    burn_width = 1500  # 75mm at 0.05 mm/px
    burn_height = 1080  # 53.98mm at 0.05 mm/px

    # Create canvas
    canvas = Image.new("RGB", (burn_width, burn_height), "white")
    draw = ImageDraw.Draw(canvas)

    # Position QR to appear centered on physical card (not centered in burn area)
    # Card is 85.6mm (1712px), burn area is 75mm (1500px), difference is 212px
    # To center on card: shift right by half the difference
    card_width_px = 1712  # Full card width at 0.05 mm/px (85.6mm × 20 px/mm)
    offset_right = (card_width_px - burn_width) // 2  # ~106px shift right

    # Try to use a decent font, fall back to default
    try:
        font_large = ImageFont.truetype(
            "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf", 36
        )
        font_small = ImageFont.truetype(
            "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf", 24
        )
    except (OSError, IOError):
        font_large = ImageFont.load_default()
        font_small = ImageFont.load_default()

    # Measure text block heights
    ssid_text = f"SSID: {ssid}"
    ssid_bbox = draw.textbbox((0, 0), ssid_text, font=font_large)
    ssid_h = ssid_bbox[3] - ssid_bbox[1]

    desc_h = 0
    line_gap = 8
    if description:
        desc_bbox = draw.textbbox((0, 0), description, font=font_small)
        desc_h = desc_bbox[3] - desc_bbox[1]

    text_block_height = ssid_h + (line_gap + desc_h if desc_h else 0)
    gap = 20  # gap between QR and text

    # Compute max qr size based on available space (leave 20px margins)
    max_qr_w = burn_width - 40 - offset_right
    max_qr_h = burn_height - text_block_height - gap - 40
    qr_size = min(max_qr_w, max_qr_h)

    # Enforce minimum top/bottom clearance of 3 mm (3 / 0.05 = 60 px)
    min_margin_mm = 3
    min_margin = int(round(min_margin_mm / 0.05))

    # If enforcing margins, compute available height for QR and shrink if needed
    available_height_for_qr = burn_height - (2 * min_margin) - gap - text_block_height
    if available_height_for_qr < qr_size:
        qr_size = max(1, available_height_for_qr)

    qr_img = qr_img.resize((qr_size, qr_size), Image.Resampling.LANCZOS)
    qr_x = (
        burn_width - qr_size
    ) // 2 + offset_right  # Shifted right for card centering

    # Center the whole block (QR + gap + text) vertically respecting min margins
    total_block_height = qr_size + gap + text_block_height
    center_y = (burn_height - total_block_height) // 2
    qr_y = max(min_margin, center_y)

    # If bottom would be too small, shift up so bottom margin == min_margin
    if qr_y + total_block_height + min_margin > burn_height:
        qr_y = burn_height - total_block_height - min_margin
        if qr_y < min_margin:
            qr_y = min_margin

    # If bottom would be too small, shift up so bottom margin == min_margin
    if qr_y + total_block_height + min_margin > burn_height:
        qr_y = burn_height - total_block_height - min_margin
        if qr_y < min_margin:
            # Block too tall for margins; fall back to top margin
            qr_y = min_margin

    canvas.paste(qr_img, (qr_x, qr_y))

    # Diagnostics: computed sizes and margins
    top_margin = qr_y
    bottom_margin = burn_height - (qr_y + total_block_height)
    print(
        f"Layout: qr_size={qr_size}px ({qr_size*0.05:.2f} mm), "
        f"text_block={text_block_height}px ({text_block_height*0.05:.2f} mm)"
    )
    print(f"Total block={total_block_height}px ({total_block_height*0.05:.2f} mm)")
    print(
        f"Margins: top={top_margin}px ({top_margin*0.05:.2f} mm), "
        f"bottom={bottom_margin}px ({bottom_margin*0.05:.2f} mm) — "
        f"min {min_margin_mm} mm enforced"
    )

    # Add text at bottom (centered on card)
    text_y = qr_y + qr_size + gap

    # Draw SSID
    text_bbox = draw.textbbox((0, 0), ssid_text, font=font_large)
    text_width = text_bbox[2] - text_bbox[0]
    draw.text(
        ((burn_width - text_width) // 2 + offset_right, text_y),
        ssid_text,
        fill="black",
        font=font_large,
    )

    # Draw description if provided
    if description:
        desc_y = text_y + ssid_h + line_gap
        desc_bbox = draw.textbbox((0, 0), description, font=font_small)
        desc_width = desc_bbox[2] - desc_bbox[0]
        draw.text(
            ((burn_width - desc_width) // 2 + offset_right, desc_y),
            description,
            fill="black",
            font=font_small,
        )

    # Save
    canvas.save(output)
    print(f"Generated: {output}")
    print(f"  SSID: {ssid}")
    print(f"  Security: {security}")
    print(f"  Description: {description or '(none)'}")
    print(f"  Size: {burn_width}×{burn_height} px (75×53.98 mm @ K6 native 0.05 mm/px)")
    print(
        "  Note: Card width 85.6mm, burn width 75mm - left-align card, right edge extends beyond"
    )
    print(f"  Password: {'*' * len(password)} (in QR only)")

    return output


def generate_alignment_box(output="QR-WIFI-alignment.png"):
    """
    Generate alignment box for credit card bamboo (K6 burn area width)

    Box is 75mm wide (K6 limit), card is 85.6mm wide.
    Left-align card in box; right edge extends ~11mm beyond burn area.

    Returns:
        Path to generated image
    """
    # K6 max burn width: 75mm (conservative for 80mm limit)
    # Credit card height: 53.98mm
    # K6 native resolution: 0.05 mm/pixel = 20 pixels/mm
    burn_width = 1500  # 75mm at 0.05 mm/px
    burn_height = 1080  # 53.98mm at 0.05 mm/px

    canvas = Image.new("RGB", (burn_width, burn_height), "white")
    draw = ImageDraw.Draw(canvas)

    # Position box to appear centered on physical card
    # Card is 85.6mm (1712px), burn area is 75mm (1500px)
    card_width_px = 1712  # 85.6mm at 0.05 mm/px
    offset_right = (card_width_px - burn_width) // 2  # ~106px shift right

    # Draw box with 10px border, shifted right
    border = 10
    left = border + offset_right
    right = burn_width - border
    draw.rectangle(
        [(left, border), (right, burn_height - border)], outline="black", width=3
    )

    # Add corner marks at the actual box corners
    corner_size = 20
    for x, y in [
        (left, border),
        (right, border),
        (left, burn_height - border),
        (right, burn_height - border),
    ]:
        draw.line([(x - corner_size, y), (x + corner_size, y)], fill="black", width=2)
        draw.line([(x, y - corner_size), (x, y + corner_size)], fill="black", width=2)

    # Add label
    try:
        font = ImageFont.truetype("/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf", 24)
    except (OSError, IOError):
        font = ImageFont.load_default()

    label = "K6 Burn Area (75 × 53.98 mm)"
    bbox = draw.textbbox((0, 0), label, font=font)
    text_width = bbox[2] - bbox[0]
    draw.text(
        ((burn_width - text_width) // 2, burn_height // 2 - 12),
        label,
        fill="black",
        font=font,
    )

    canvas.save(output)
    print(f"Generated alignment box: {output}")
    print(f"  Size: {burn_width}×{burn_height} px (75×53.98 mm @ K6 native 0.05 mm/px)")
    print(
        "  Note: Credit card is 85.6mm wide - left-align card, right ~11mm extends beyond"
    )

    return output


def main():
    parser = argparse.ArgumentParser(
        description="Generate WiFi QR code for laser engraving on credit card sized bamboo",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  # Generate WiFi QR with description
  ./generate_qr_wifi.py -s MyWiFi -p SecretPass123 -d "Guest Network"

  # Generate alignment box only
  ./generate_qr_wifi.py --alignment

  # Hidden network
  ./generate_qr_wifi.py -s Hidden_SSID -p Pass456 --hidden
        """,
    )

    parser.add_argument("-s", "--ssid", help="WiFi SSID (network name)")
    parser.add_argument(
        "-p", "--password", help="WiFi password (NOT displayed, only in QR)"
    )
    parser.add_argument(
        "-t",
        "--security",
        default="WPA",
        choices=["WPA", "WEP", "nopass"],
        help="Security type (default: WPA)",
    )
    parser.add_argument("-d", "--description", help="Description text to display")
    parser.add_argument("--hidden", action="store_true", help="SSID is hidden")
    parser.add_argument(
        "-o",
        "--output",
        help="Output filename (default: QR-WIFI-YYMMDDHHMMSS.png or QR-WIFI-alignment.png)",
    )
    parser.add_argument(
        "--alignment", action="store_true", help="Generate alignment box instead of QR"
    )

    args = parser.parse_args()

    if args.alignment:
        output = args.output if args.output else "QR-WIFI-alignment.png"
        generate_alignment_box(output)
    else:
        if not args.ssid or not args.password:
            parser.error("--ssid and --password required (or use --alignment)")

        generate_wifi_qr(
            ssid=args.ssid,
            password=args.password,
            security=args.security,
            hidden=args.hidden,
            description=args.description,
            output=args.output,
        )


if __name__ == "__main__":
    main()
