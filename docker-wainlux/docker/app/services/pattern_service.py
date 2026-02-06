"""Pattern generation service for calibration and testing"""

from PIL import Image, ImageDraw


class PatternService:
    """Generate test patterns for calibration"""

    @staticmethod
    def generate(pattern: str, resolution: float = 0.05, size_mm: float = 10.0) -> Image.Image:
        """Generate calibration pattern.
        
        Args:
            pattern: Pattern type (center, corners, frame, grid, bottom-test)
            resolution: mm per pixel
            size_mm: Pattern size in millimeters
            
        Returns:
            PIL Image in mode "1" (1-bit black/white)
        """
        def mm_to_px(mm: float) -> int:
            return int(round(mm / resolution))

        if pattern == "center":
            return PatternService._center(mm_to_px, size_mm)
        elif pattern == "corners":
            return PatternService._corners(mm_to_px, size_mm)
        elif pattern == "frame":
            return PatternService._frame(mm_to_px)
        elif pattern == "grid":
            return PatternService._grid(mm_to_px, size_mm)
        elif pattern == "bottom-test":
            return PatternService._bottom_test(mm_to_px)
        else:
            raise ValueError(f"Unknown pattern: {pattern}")

    @staticmethod
    def _center(mm_to_px, size_mm: float) -> Image.Image:
        """Center pattern: small box with crosshair, centered in work area"""
        size_px = mm_to_px(size_mm)
        work_area_px = mm_to_px(80.0)
        
        # Small box
        box = Image.new("1", (size_px, size_px), 1)
        draw = ImageDraw.Draw(box)
        draw.rectangle((0, 0, size_px - 1, size_px - 1), outline=0, width=2)
        
        # Crosshairs
        center = size_px // 2
        cross_half = mm_to_px(2.5)
        draw.line((center - cross_half, center, center + cross_half, center), fill=0, width=1)
        draw.line((center, center - cross_half, center, center + cross_half), fill=0, width=1)
        
        # Center in work area
        img = Image.new("1", (work_area_px, work_area_px), 1)
        offset = (work_area_px - size_px) // 2
        img.paste(box, (offset, offset))
        return img

    @staticmethod
    def _corners(mm_to_px, size_mm: float) -> Image.Image:
        work_area_px = mm_to_px(80.0)
        box_px = mm_to_px(size_mm)
        img = Image.new("1", (work_area_px, work_area_px), 1)

        # Create box template
        box = Image.new("1", (box_px, box_px), 1)
        draw_box = ImageDraw.Draw(box)
        draw_box.rectangle((0, 0, box_px - 1, box_px - 1), outline=0, width=2)

        # Place in corners
        for x, y in [(0, 0), (work_area_px - box_px, 0), 
                     (0, work_area_px - box_px), (work_area_px - box_px, work_area_px - box_px)]:
            img.paste(box, (x, y))

        # Border
        draw = ImageDraw.Draw(img)
        draw.rectangle((0, 0, work_area_px - 1, work_area_px - 1), outline=0, width=1)
        return img

    @staticmethod
    def _frame(mm_to_px) -> Image.Image:
        size_px = mm_to_px(80.0)
        img = Image.new("1", (size_px, size_px), 1)
        draw = ImageDraw.Draw(img)
        draw.rectangle((0, 0, size_px - 1, size_px - 1), outline=0, width=2)
        return img

    @staticmethod
    def _grid(mm_to_px, size_mm: float) -> Image.Image:
        work_area_px = mm_to_px(80.0)
        grid_spacing_px = mm_to_px(size_mm)
        img = Image.new("1", (work_area_px, work_area_px), 1)
        draw = ImageDraw.Draw(img)

        # Vertical + horizontal lines
        for x in range(0, work_area_px + 1, grid_spacing_px):
            draw.line((x, 0, x, work_area_px - 1), fill=0, width=1)
        for y in range(0, work_area_px + 1, grid_spacing_px):
            draw.line((0, y, work_area_px - 1, y), fill=0, width=1)

        # Thicker border
        draw.rectangle((0, 0, work_area_px - 1, work_area_px - 1), outline=0, width=2)
        return img

    @staticmethod
    def _bottom_test(mm_to_px) -> Image.Image:
        work_area_px = mm_to_px(80.0)
        img = Image.new("1", (work_area_px, work_area_px), 1)
        draw = ImageDraw.Draw(img)

        # Diagonal line in bottom 1/8th (Y-axis limit test)
        bottom_eighth = work_area_px // 8
        start_y = work_area_px - bottom_eighth
        draw.line((0, start_y, work_area_px - 1, work_area_px - 1), fill=0, width=3)
        return img

    @staticmethod
    def generate_alignment(width_mm: float, height_mm: float, 
                          resolution: float = 0.05) -> tuple[Image.Image, dict]:
        """Generate custom alignment box for object positioning.
        
        Args:
            width_mm: Object width in millimeters
            height_mm: Object height in millimeters
            resolution: mm per pixel (default 0.05)
            
        Returns:
            Tuple of (PIL Image mode "1", metadata dict with center_x, center_y)
        """
        def mm_to_px(mm: float) -> int:
            return int(round(mm / resolution))
        
        # Validate dimensions
        max_width_mm = 80.0
        max_height_mm = 76.0
        if width_mm > max_width_mm or height_mm > max_height_mm:
            raise ValueError(
                f"Dimensions exceed K6 limits: {width_mm}×{height_mm}mm "
                f"(max {max_width_mm}×{max_height_mm}mm)"
            )
        
        width_px = mm_to_px(width_mm)
        height_px = mm_to_px(height_mm)
        
        # Add margin for corner marks outside object boundary
        cross_size = 30  # 1.5mm at 0.05mm/px
        margin = 5       # Gap between border and cross
        canvas_margin = cross_size + margin
        
        # Create canvas with extra space for corner marks
        canvas_width = width_px + (2 * canvas_margin)
        canvas_height = height_px + (2 * canvas_margin)
        img = Image.new("1", (canvas_width, canvas_height), 1)  # White background
        draw = ImageDraw.Draw(img)
        
        # Border at object dimensions (offset by canvas_margin)
        box_left = canvas_margin
        box_top = canvas_margin
        box_right = canvas_margin + width_px - 1
        box_bottom = canvas_margin + height_px - 1
        draw.rectangle((box_left, box_top, box_right, box_bottom), outline=0, width=2)
        
        # Corner crosses (OUTSIDE boundary, visible for alignment)
        # Top-left
        draw.line([(box_left - margin, box_top - margin - cross_size), (box_left - margin, box_top - margin)], fill=0, width=2)
        draw.line([(box_left - margin - cross_size, box_top - margin), (box_left - margin, box_top - margin)], fill=0, width=2)
        
        # Top-right
        draw.line([(box_right + margin, box_top - margin - cross_size), (box_right + margin, box_top - margin)], fill=0, width=2)
        draw.line([(box_right + margin, box_top - margin), (box_right + margin + cross_size, box_top - margin)], fill=0, width=2)
        
        # Bottom-left
        draw.line([(box_left - margin, box_bottom + margin), (box_left - margin, box_bottom + margin + cross_size)], fill=0, width=2)
        draw.line([(box_left - margin - cross_size, box_bottom + margin), (box_left - margin, box_bottom + margin)], fill=0, width=2)
        
        # Bottom-right
        draw.line([(box_right + margin, box_bottom + margin), (box_right + margin, box_bottom + margin + cross_size)], fill=0, width=2)
        draw.line([(box_right + margin, box_bottom + margin), (box_right + margin + cross_size, box_bottom + margin)], fill=0, width=2)
        
        # Center coordinates relative to full canvas
        center_x = canvas_width // 2
        center_y = canvas_height // 2
        
        metadata = {
            "width_px": canvas_width,
            "height_px": canvas_height,
            "object_width_px": width_px,
            "object_height_px": height_px,
            "width_mm": width_mm,
            "height_mm": height_mm,
            "center_x": center_x,
            "center_y": center_y,
            "resolution": resolution
        }
        
        return img, metadata

    @staticmethod
    def generate_card_alignment(burn_width_mm: float = 75.0, burn_height_mm: float = 53.98,
                               card_width_mm: float = 85.6, resolution: float = 0.05) -> tuple[Image.Image, dict]:
        """Generate alignment box for credit card with offset burn area.
        
        Credit cards (85.6mm × 53.98mm) are wider than K6 burn area (75mm × 53.98mm).
        Burn area is offset from card center. Right side extends beyond burn area,
        so only left side gets alignment marks.
        
        Args:
            burn_width_mm: Burn area width (default 75mm)
            burn_height_mm: Burn area height (default 53.98mm)
            card_width_mm: Card width (default 85.6mm)
            resolution: mm per pixel (default 0.05)
            
        Returns:
            Tuple of (PIL Image mode "1", metadata dict)
        """
        def mm_to_px(mm: float) -> int:
            return int(round(mm / resolution))
        
        burn_width_px = mm_to_px(burn_width_mm)
        burn_height_px = mm_to_px(burn_height_mm)
        card_width_px = mm_to_px(card_width_mm)
        
        # Calculate offset (burn area positioning on card)
        offset_right = (card_width_px - burn_width_px) // 2
        
        # Canvas dimensions (card size + left margin for crosses)
        cross_size = 30
        margin = 5
        left_margin = cross_size + margin
        
        canvas_width = card_width_px + left_margin
        canvas_height = burn_height_px + (2 * (cross_size + margin))
        img = Image.new("1", (canvas_width, canvas_height), 1)
        draw = ImageDraw.Draw(img)
        
        # Burn area boundary (offset from left by left_margin + offset_right)
        burn_left = left_margin + offset_right
        burn_top = cross_size + margin
        burn_right = burn_left + burn_width_px - 1
        burn_bottom = burn_top + burn_height_px - 1
        
        # Card left edge (positioning reference)
        card_left = left_margin
        draw.line([(card_left, burn_top), (card_left, burn_bottom)], fill=0, width=2)
        
        # Burn area markers: top and bottom edges only
        draw.line([(card_left, burn_top), (burn_right, burn_top)], fill=0, width=2)  # Top
        draw.line([(card_left, burn_bottom), (burn_right, burn_bottom)], fill=0, width=2)  # Bottom
        
        # Corner crosses OUTSIDE card left edge (for card positioning)
        # Top-left
        draw.line([(card_left - margin, burn_top - margin - cross_size), (card_left - margin, burn_top - margin)], fill=0, width=2)
        draw.line([(card_left - margin - cross_size, burn_top - margin), (card_left - margin, burn_top - margin)], fill=0, width=2)
        
        # Bottom-left
        draw.line([(card_left - margin, burn_bottom + margin), (card_left - margin, burn_bottom + margin + cross_size)], fill=0, width=2)
        draw.line([(card_left - margin - cross_size, burn_bottom + margin), (card_left - margin, burn_bottom + margin)], fill=0, width=2)
        
        # Center coordinates (relative to burn area)
        center_x = burn_left + (burn_width_px // 2)
        center_y = burn_top + (burn_height_px // 2)
        
        metadata = {
            "canvas_width": canvas_width,
            "canvas_height": canvas_height,
            "burn_width_px": burn_width_px,
            "burn_height_px": burn_height_px,
            "card_width_px": card_width_px,
            "burn_offset_x": burn_left,
            "burn_offset_y": burn_top,
            "center_x": center_x,
            "center_y": center_y,
            "resolution": resolution
        }
        
        return img, metadata
