"""QR code generation service"""

import qrcode
import io
import base64
import logging
from PIL import Image, ImageDraw, ImageFont

logger = logging.getLogger(__name__)


class QRService:
    """Service for generating WiFi QR codes"""

    @staticmethod
    def generate_wifi_qr(ssid: str, password: str, security: str = "WPA", 
                         description: str = "", show_password: bool = False) -> tuple[Image.Image, dict]:
        """Generate WiFi QR code with text labels (tight bounding box).

        Creates QR code + text with minimal canvas (no positioning).
        Layout system handles placement on material/burn area.

        Args:
            ssid: WiFi network SSID
            password: WiFi password
            security: Security type (WPA, WEP, or empty)
            description: Optional description text
            show_password: If True, display password as text in image

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

        qr.add_data(wifi_data)
        qr.make(fit=True)
        qr_img = qr.make_image(fill_color="black", back_color="white")

        # Load fonts - sizes for readability at 0.05mm/px
        # 60px = 3mm, 40px = 2mm
        try:
            font_large = ImageFont.truetype(
                "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf", 60
            )
            font_small = ImageFont.truetype(
                "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf", 40
            )
        except (OSError, IOError):
            logger.warning("DejaVu fonts missing; falling back to default.")
            font_large = ImageFont.load_default()
            font_small = ImageFont.load_default()

        # Measure text to calculate canvas size
        ssid_text = f"SSID: {ssid}"
        
        # Create temporary draw context for measurement
        temp_img = Image.new("RGB", (1, 1), "white")
        temp_draw = ImageDraw.Draw(temp_img)
        
        ssid_bbox = temp_draw.textbbox((0, 0), ssid_text, font=font_large)
        ssid_w = ssid_bbox[2] - ssid_bbox[0]
        ssid_h = ssid_bbox[3] - ssid_bbox[1]

        # Measure password if showing
        pwd_w = 0
        pwd_h = 0
        pwd_text = ""
        if show_password and password:
            pwd_text = f"Password: {password}"
            pwd_bbox = temp_draw.textbbox((0, 0), pwd_text, font=font_small)
            pwd_w = pwd_bbox[2] - pwd_bbox[0]
            pwd_h = pwd_bbox[3] - pwd_bbox[1]

        desc_w = 0
        desc_h = 0
        if description:
            desc_bbox = temp_draw.textbbox((0, 0), description, font=font_small)
            desc_w = desc_bbox[2] - desc_bbox[0]
            desc_h = desc_bbox[3] - desc_bbox[1]

        # Calculate tight canvas size
        line_gap = 16  # Gap between text lines
        text_gap = 30  # Gap between QR and text
        margin = 20    # Small margin around content
        
        qr_size = qr_img.size[0]  # QR is square
        
        # Calculate text block height (SSID + password + description)
        text_block_height = ssid_h
        if show_password and password:
            text_block_height += line_gap + pwd_h
        if description:
            text_block_height += line_gap + desc_h
        
        canvas_width = max(qr_size, ssid_w, pwd_w, desc_w) + (2 * margin)
        canvas_height = qr_size + text_gap + text_block_height + (2 * margin)
        
        # Create tight canvas
        canvas = Image.new("RGB", (canvas_width, canvas_height), "white")
        draw = ImageDraw.Draw(canvas)
        
        # Center QR horizontally (convert 1-bit → RGB before pasting)
        qr_rgb = qr_img.convert("RGB")
        qr_x = (canvas_width - qr_size) // 2
        qr_y = margin
        canvas.paste(qr_rgb, (qr_x, qr_y))
        
        # Center text below QR
        text_y = qr_y + qr_size + text_gap
        ssid_x = (canvas_width - ssid_w) // 2
        draw.text((ssid_x, text_y), ssid_text, fill="black", font=font_large)
        
        # Add password if showing
        if show_password and password:
            pwd_y = text_y + ssid_h + line_gap
            pwd_x = (canvas_width - pwd_w) // 2
            draw.text((pwd_x, pwd_y), pwd_text, fill="black", font=font_small)
            text_y = pwd_y  # Update for next line
        
        if description:
            desc_y = text_y + (pwd_h if show_password and password else ssid_h) + line_gap
            desc_x = (canvas_width - desc_w) // 2
            draw.text((desc_x, desc_y), description, fill="black", font=font_small)

        # CRITICAL: Convert to 1-bit to remove font antialiasing
        # ImageDraw.text() produces antialiased RGB even on white background
        # Threshold at 128 to binarize (< 128 = black, >= 128 = white)
        gray = canvas.convert("L")
        canvas = gray.point(lambda x: 0 if x < 128 else 255, mode="1")

        metadata = {
            "width": canvas_width,
            "height": canvas_height,
            "qr_size": qr_size,
            "ssid": ssid,
            "security": security,
        }

        return canvas, metadata

    @staticmethod
    def _unused_generate_alignment_box() -> Image.Image:
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
