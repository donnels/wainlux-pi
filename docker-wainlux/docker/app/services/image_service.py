"""Image processing service for K6 laser engraving"""

import numpy as np
from PIL import Image
import io
import subprocess
import tempfile
from pathlib import Path


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

    @staticmethod
    def svg_to_png(svg_path: str, width: int = 800, dpi: int = 96) -> Image.Image:
        """Convert SVG to PNG using Inkscape.
        
        Args:
            svg_path: Path to SVG file
            width: Target width in pixels (height auto-calculated from aspect ratio)
            dpi: Rasterization DPI (affects quality)
            
        Returns:
            PIL Image (RGB mode)
            
        Raises:
            RuntimeError: If Inkscape not available or conversion fails
        """
        try:
            # Check for Inkscape
            result = subprocess.run(
                ['inkscape', '--version'],
                capture_output=True,
                text=True,
                timeout=5
            )
            if result.returncode != 0:
                raise RuntimeError("Inkscape not available")
                
        except (subprocess.TimeoutExpired, FileNotFoundError):
            raise RuntimeError("Inkscape not installed. Install: apt-get install inkscape")
        
        # Create temp PNG file
        with tempfile.NamedTemporaryFile(suffix='.png', delete=False) as tmp:
            png_path = tmp.name
        
        try:
            # Convert SVG → PNG using Inkscape
            # --export-width sets width, height auto-scaled
            # --export-background=white for laser (burn=black on white)
            result = subprocess.run(
                [
                    'inkscape',
                    svg_path,
                    '--export-type=png',
                    f'--export-filename={png_path}',
                    f'--export-width={width}',
                    '--export-background=white',
                    '--export-background-opacity=1.0'
                ],
                capture_output=True,
                text=True,
                timeout=30
            )
            
            if result.returncode != 0:
                raise RuntimeError(f"Inkscape conversion failed: {result.stderr}")
            
            # Load PNG
            img = Image.open(png_path)
            return img
            
        finally:
            # Clean up temp file
            Path(png_path).unlink(missing_ok=True)
    
    @staticmethod
    def image_to_base64(img: Image.Image) -> str:
        """Convert PIL Image to base64 data URL for preview.
        
        Generic preview conversion - works for ANY image (QR, calibration, uploads).
        All images end up as photons from the laser, preview should be unified.
        
        Args:
            img: PIL Image to convert
            
        Returns:
            Base64 data URL string
        """
        import base64
        
        buffer = io.BytesIO()
        img.save(buffer, format="PNG")
        img_base64 = base64.b64encode(buffer.getvalue()).decode()
        return f"data:image/png;base64,{img_base64}"

    @staticmethod
    def center_image_at(img: Image.Image, target_width: int, target_height: int,
                       center_x: int, center_y: int) -> Image.Image:
        """Center image at specific coordinates within target canvas.
        
        Used with alignment boxes: burn alignment, get center coordinates,
        then use those coordinates to position the actual burn image.
        
        Args:
            img: PIL Image to center (any mode)
            target_width: Canvas width in pixels
            target_height: Canvas height in pixels
            center_x: Center X coordinate in pixels
            center_y: Center Y coordinate in pixels
            
        Returns:
            PIL Image on white canvas, centered at specified coordinates
        """
        # Create canvas matching input mode
        canvas = Image.new(img.mode, (target_width, target_height), 
                          255 if img.mode in ("1", "L") else "white")
        
        # Calculate paste position (top-left corner)
        paste_x = center_x - (img.width // 2)
        paste_y = center_y - (img.height // 2)
        
        # Validate positioning
        if paste_x < 0 or paste_y < 0:
            raise ValueError(
                f"Image extends beyond canvas top-left: "
                f"paste=({paste_x},{paste_y}), size={img.size}"
            )
        if paste_x + img.width > target_width or paste_y + img.height > target_height:
            raise ValueError(
                f"Image extends beyond canvas bottom-right: "
                f"paste=({paste_x},{paste_y}), size={img.size}, canvas=({target_width},{target_height})"
            )
        
        canvas.paste(img, (paste_x, paste_y))
        return canvas
