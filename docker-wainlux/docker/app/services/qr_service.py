"""QR code generation service"""

import qrcode
import io
import base64
import logging
from PIL import Image, ImageDraw, ImageFont

logger = logging.getLogger(__name__)


class QRService:
    """Service for generating WiFi QR codes"""

    # WiFi QR card dimensions (credit card positioning)
    BURN_WIDTH = 1500  # px (75mm at 0.05mm/px)
    BURN_HEIGHT = 1080  # px (53.98mm)
    CARD_WIDTH_PX = 1712  # px (85.6mm actual card width)
    OFFSET_RIGHT = (CARD_WIDTH_PX - BURN_WIDTH) // 2  # ~106px shift

    @staticmethod
    def generate_wifi_qr(ssid: str, password: str, security: str = "WPA", 
                         description: str = "") -> tuple[Image.Image, dict]:
        """Generate WiFi QR code with text labels.

        Args:
            ssid: WiFi network SSID
            password: WiFi password
            security: Security type (WPA, WEP, or empty)
            description: Optional description text

        Returns:
            Tuple of (PIL Image, metadata dict)
        """
        if not ssid:
            raise ValueError("SSID required")

        # WiFi QR format
        wifi_data = f"WIFI:T:{security};S:{ssid};P:{password};H:false;;"

        # Generate QR with high error correction
        qr = qrcode.QRCode(
            version=None,
            error_correction=qrcode.constants.ERROR_CORRECT_H,
            box_size=10,
            border=2,
        )
        qr.add_data(wifi_data)
        qr.make(fit=True)
        qr_img = qr.make_image(fill_color="black", back_color="white")

        # Create canvas
        canvas = Image.new("RGB", (QRService.BURN_WIDTH, QRService.BURN_HEIGHT), "white")
        draw = ImageDraw.Draw(canvas)

        # Load fonts
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

        # Measure text
        ssid_text = f"SSID: {ssid}"
        ssid_bbox = draw.textbbox((0, 0), ssid_text, font=font_large)
        ssid_h = ssid_bbox[3] - ssid_bbox[1]

        desc_h = 0
        line_gap = 8
        if description:
            desc_bbox = draw.textbbox((0, 0), description, font=font_small)
            desc_h = desc_bbox[3] - desc_bbox[1]

        text_block_height = ssid_h + (line_gap + desc_h if desc_h else 0)
        gap = 20
        min_margin = 60  # 3mm at 0.05 mm/px

        # Calculate QR size
        max_qr_w = QRService.BURN_WIDTH - 40 - QRService.OFFSET_RIGHT
        available_height = QRService.BURN_HEIGHT - (2 * min_margin) - gap - text_block_height
        qr_size = min(max_qr_w, available_height)

        qr_img = qr_img.resize((qr_size, qr_size), Image.Resampling.LANCZOS)

        # Position QR
        total_block_height = qr_size + gap + text_block_height
        qr_y = max(min_margin, (QRService.BURN_HEIGHT - total_block_height) // 2)
        if qr_y + total_block_height + min_margin > QRService.BURN_HEIGHT:
            qr_y = QRService.BURN_HEIGHT - total_block_height - min_margin
            if qr_y < min_margin:
                qr_y = min_margin

        qr_x = (QRService.BURN_WIDTH - qr_size) // 2 + QRService.OFFSET_RIGHT
        canvas.paste(qr_img, (qr_x, qr_y))

        # Add text
        text_y = qr_y + qr_size + gap
        text_bbox = draw.textbbox((0, 0), ssid_text, font=font_large)
        text_width = text_bbox[2] - text_bbox[0]
        draw.text(
            ((QRService.BURN_WIDTH - text_width) // 2 + QRService.OFFSET_RIGHT, text_y),
            ssid_text,
            fill="black",
            font=font_large,
        )

        if description:
            desc_y = text_y + ssid_h + line_gap
            desc_bbox = draw.textbbox((0, 0), description, font=font_small)
            desc_width = desc_bbox[2] - desc_bbox[0]
            draw.text(
                ((QRService.BURN_WIDTH - desc_width) // 2 + QRService.OFFSET_RIGHT, desc_y),
                description,
                fill="black",
                font=font_small,
            )

        metadata = {
            "width": QRService.BURN_WIDTH,
            "height": QRService.BURN_HEIGHT,
            "qr_size": qr_size,
            "ssid": ssid,
            "security": security,
        }

        return canvas, metadata

    @staticmethod
    def generate_qr_preview_base64(ssid: str, password: str, security: str = "WPA",
                                   description: str = "") -> tuple[str, dict]:
        """Generate WiFi QR code and return as base64 data URL for preview.

        Args:
            ssid: WiFi network SSID
            password: WiFi password
            security: Security type
            description: Optional description

        Returns:
            Tuple of (base64 data URL, metadata dict)
        """
        canvas, metadata = QRService.generate_wifi_qr(ssid, password, security, description)

        # Convert to base64
        buffer = io.BytesIO()
        canvas.save(buffer, format="PNG")
        img_base64 = base64.b64encode(buffer.getvalue()).decode()
        data_url = f"data:image/png;base64,{img_base64}"

        return data_url, metadata

    @staticmethod
    def generate_alignment_box() -> Image.Image:
        """Generate alignment box for credit card positioning (75mm × 53.98mm).

        Returns:
            PIL Image
        """
        canvas = Image.new("RGB", (QRService.BURN_WIDTH, QRService.BURN_HEIGHT), "white")
        draw = ImageDraw.Draw(canvas)

        # Card positioning offset
        offset_right = QRService.OFFSET_RIGHT

        # Draw box with border
        border = 10
        left = border + offset_right
        right = QRService.BURN_WIDTH - border
        draw.rectangle(
            [(left, border), (right, QRService.BURN_HEIGHT - border)],
            outline="black",
            width=3
        )

        # Corner marks
        corner_size = 20
        for x, y in [
            (left, border),
            (right, border),
            (left, QRService.BURN_HEIGHT - border),
            (right, QRService.BURN_HEIGHT - border),
        ]:
            draw.line([(x - corner_size, y), (x + corner_size, y)], fill="black", width=2)
            draw.line([(x, y - corner_size), (x, y + corner_size)], fill="black", width=2)

        # Label
        try:
            font = ImageFont.truetype(
                "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf", 24
            )
        except (OSError, IOError):
            font = ImageFont.load_default()

        label = "K6 Burn Area (75 × 53.98 mm)"
        bbox = draw.textbbox((0, 0), label, font=font)
        text_width = bbox[2] - bbox[0]
        draw.text(
            ((QRService.BURN_WIDTH - text_width) // 2, QRService.BURN_HEIGHT // 2 - 12),
            label,
            fill="black",
            font=font,
        )

        return canvas
