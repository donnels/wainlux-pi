"""Image processing service for K6 laser engraving"""

import numpy as np
from PIL import Image


class ImageService:
    """Image processing operations for laser engraving"""

    @staticmethod
    def color_aware_grayscale(img: Image.Image, laser_color: str = "blue") -> Image.Image:
        """Convert RGB image to grayscale optimized for laser wavelength.

        Args:
            img: PIL Image in RGB mode
            laser_color: 'blue', 'red', 'green', or 'neutral'

        Returns:
            PIL Image in 'L' mode (grayscale)

        Color wavelengths absorb differently:
        - Blue laser (450nm): Absorbs red+green, reflects blue
        - Red laser (650nm): Absorbs blue+green, reflects red
        - Green laser (532nm): Absorbs red+blue, reflects green

        For best engraving contrast, emphasize colors the laser absorbs.
        """
        if img.mode != "RGB":
            img = img.convert("RGB")

        arr = np.array(img, dtype=np.float32)
        r, g, b = arr[:, :, 0], arr[:, :, 1], arr[:, :, 2]

        if laser_color == "blue":
            # Blue laser: emphasize R+G channels (absorb), de-emphasize B (reflects)
            gray = 0.45 * r + 0.45 * g + 0.10 * b
        elif laser_color == "red":
            # Red laser: emphasize B+G channels, de-emphasize R
            gray = 0.10 * r + 0.45 * g + 0.45 * b
        elif laser_color == "green":
            # Green laser: emphasize R+B channels, de-emphasize G
            gray = 0.45 * r + 0.10 * g + 0.45 * b
        else:  # neutral
            # Standard luminance conversion
            gray = 0.299 * r + 0.587 * g + 0.114 * b

        gray = np.clip(gray, 0, 255).astype(np.uint8)
        return Image.fromarray(gray, mode="L")

    @staticmethod
    def validate_image_size(width: int, height: int, max_width: int, max_height: int) -> tuple[bool, str]:
        """Validate image dimensions against maximum size.

        Args:
            width: Image width in pixels
            height: Image height in pixels
            max_width: Maximum allowed width
            max_height: Maximum allowed height

        Returns:
            Tuple of (valid, error_message)
        """
        if width > max_width or height > max_height:
            return False, f"Image too large: {width}x{height}px (max {max_width}x{max_height})"
        return True, ""

    @staticmethod
    def allowed_file(filename: str, allowed_extensions: set[str]) -> bool:
        """Check if file extension is allowed.

        Args:
            filename: Name of file to check
            allowed_extensions: Set of allowed extensions (lowercase, no dots)

        Returns:
            True if extension is allowed
        """
        return "." in filename and filename.rsplit(".", 1)[1].lower() in allowed_extensions
