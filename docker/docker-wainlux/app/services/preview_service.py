"""
Preview Service - Multi-layer composite renderer

Renders visual previews for all pipeline stages:
- upload: Image on burn area
- process: Processed image (1-bit) on burn area
- layout: Burn area + material + image + crop indicators + annotations

Used by single-step mode for visual debugging.
"""

from PIL import Image, ImageDraw, ImageFont
from pathlib import Path
import numpy as np
from typing import Dict, List, Tuple, Optional
import logging
from constants import K6Constants

logger = logging.getLogger(__name__)


class PreviewService:
    """Multi-layer preview renderer for K6 jobs"""
    
    # Preview canvas size (larger than burn area for annotations)
    CANVAS_WIDTH = 2000
    CANVAS_HEIGHT = 2000
    
    # Colors
    COLOR_WORKSPACE = "#E0E0E0"  # Light gray
    COLOR_BURN_AREA = "#FFFFFF"  # White
    COLOR_BURN_BORDER = "#FF0000"  # Red (reference frame, not burned)
    COLOR_MATERIAL = "#0066CC"  # Blue
    COLOR_IMAGE = "#00CC00"  # Green (image boundary)
    COLOR_CROP = "#FF0000"  # Red
    COLOR_ANNOTATION = "#333333"  # Dark gray
    COLOR_WARNING = "#FF8800"  # Orange
    COLOR_ERROR = "#CC0000"  # Red
    COLOR_SUCCESS = "#00AA00"  # Green
    
    @staticmethod
    def render_preview(job: dict, stage: str = "layout", options: dict = None) -> Image.Image:
        """
        Render multi-layer preview for job at given stage
        
        Args:
            job: Job dict (partial or complete .k6job format)
            stage: Which stage to preview (upload/process/layout/all)
            options: Display options (show_layers, show_grid, show_annotations)
        
        Returns:
            PIL Image (composite preview)
        """
        options = options or {}
        show_layers = options.get("show_layers", ["burn_area", "material", "image", "annotations"])
        show_grid = options.get("show_grid", False)
        show_annotations = options.get("show_annotations", True)
        
        # Create canvas
        canvas = Image.new("RGB", (PreviewService.CANVAS_WIDTH, PreviewService.CANVAS_HEIGHT), 
                          PreviewService.COLOR_WORKSPACE)
        draw = ImageDraw.Draw(canvas)
        
        # Calculate burn area position (centered on canvas)
        burn_offset_x = (PreviewService.CANVAS_WIDTH - K6Constants.BURN_WIDTH_PX) // 2
        burn_offset_y = (PreviewService.CANVAS_HEIGHT - K6Constants.BURN_HEIGHT_PX) // 2
        
        # Layer 1: Burn area
        if "burn_area" in show_layers:
            PreviewService._draw_burn_area(draw, burn_offset_x, burn_offset_y)
        
        # Layer 2: Grid (optional)
        if show_grid:
            PreviewService._draw_grid(draw, burn_offset_x, burn_offset_y)
        
        # Layer 3: Material outline (if present)
        if "material" in show_layers and "material" in job:
            material = job.get("material", {}).get("target")
            if material:
                PreviewService._draw_material(draw, canvas, material, job.get("layout", {}), 
                                             burn_offset_x, burn_offset_y)
        
        # Layer 4: Image content
        if "image" in show_layers and "stages" in job:
            upload_stage = job.get("stages", {}).get("upload")
            if upload_stage:
                PreviewService._draw_image(canvas, upload_stage, job.get("layout", {}), 
                                          burn_offset_x, burn_offset_y)
        
        # Layer 5: Crop indicators
        if "crop" in show_layers:
            crop_regions = PreviewService._detect_crops(job)
            for region in crop_regions:
                PreviewService._draw_crop_indicator(draw, region, burn_offset_x, burn_offset_y)
        
        # Layer 6: Annotations
        if show_annotations and "annotations" in show_layers:
            annotations = PreviewService._generate_annotations(job)
            PreviewService._draw_annotations(draw, annotations)
        
        return canvas
    
    @staticmethod
    def _draw_burn_area(draw: ImageDraw.Draw, offset_x: int, offset_y: int):
        """Draw burn area with external reference frame
        
        The burn area itself is white (what would be burned).
        The reference frame (red dashed) is drawn OUTSIDE to show boundaries.
        """
        x1 = offset_x
        y1 = offset_y
        x2 = offset_x + K6Constants.BURN_WIDTH_PX
        y2 = offset_y + K6Constants.BURN_HEIGHT_PX
        
        # White fill (burn area)
        draw.rectangle([x1, y1, x2, y2], fill=PreviewService.COLOR_BURN_AREA)
        
        # Red dashed border OUTSIDE burn area (reference frame)
        border_offset = 5  # px outside burn area
        ref_x1 = x1 - border_offset
        ref_y1 = y1 - border_offset
        ref_x2 = x2 + border_offset
        ref_y2 = y2 + border_offset
        
        PreviewService._draw_dashed_rect(
            draw, 
            [ref_x1, ref_y1, ref_x2, ref_y2],
            PreviewService.COLOR_BURN_BORDER,
            width=2
        )
        
        # Label OUTSIDE burn area (top-left, above border)
        try:
            font = ImageFont.truetype("/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf", 14)
        except (OSError, IOError):
            font = ImageFont.load_default()
        
        label = f"Burn Area ({K6Constants.BURN_WIDTH_MM}×{K6Constants.BURN_HEIGHT_MM} mm)"
        draw.text((ref_x1, ref_y1 - 20), label, fill=PreviewService.COLOR_BURN_BORDER, font=font)
    
    @staticmethod
    def _draw_grid(draw: ImageDraw.Draw, offset_x: int, offset_y: int, spacing_mm: float = 10.0):
        """Draw grid overlay (10mm default)"""
        spacing_px = int(spacing_mm / K6Constants.RESOLUTION_MM_PX)
        
        # Vertical lines
        x = offset_x
        while x <= offset_x + K6Constants.BURN_WIDTH_PX:
            draw.line([(x, offset_y), (x, offset_y + K6Constants.BURN_HEIGHT_PX)], 
                     fill="#CCCCCC", width=1)
            x += spacing_px
        
        # Horizontal lines
        y = offset_y
        while y <= offset_y + K6Constants.BURN_HEIGHT_PX:
            draw.line([(offset_x, y), (offset_x + K6Constants.BURN_WIDTH_PX, y)], 
                     fill="#CCCCCC", width=1)
            y += spacing_px
    
    @staticmethod
    def _draw_material(draw: ImageDraw.Draw, canvas: Image.Image, 
                      material: dict, layout: dict, offset_x: int, offset_y: int):
        """Draw material outline"""
        # Get material dimensions
        width_mm = material.get("width_mm", 0)
        height_mm = material.get("height_mm", 0)
        
        if width_mm == 0 or height_mm == 0:
            return
        
        width_px = int(width_mm / K6Constants.RESOLUTION_MM_PX)
        height_px = int(height_mm / K6Constants.RESOLUTION_MM_PX)
        
        # Get material position on burn area
        mat_layout = layout.get("material_on_burn_area", {})
        center_x_mm = mat_layout.get("center_x_mm", K6Constants.BURN_WIDTH_MM / 2)
        center_y_mm = mat_layout.get("center_y_mm", K6Constants.BURN_HEIGHT_MM / 2)
        
        center_x_px = int(center_x_mm / K6Constants.RESOLUTION_MM_PX)
        center_y_px = int(center_y_mm / K6Constants.RESOLUTION_MM_PX)
        
        # Calculate rectangle
        x1 = offset_x + center_x_px - (width_px // 2)
        y1 = offset_y + center_y_px - (height_px // 2)
        x2 = x1 + width_px
        y2 = y1 + height_px
        
        # Draw dashed outline (blue)
        PreviewService._draw_dashed_rect(draw, [x1, y1, x2, y2], 
                                        PreviewService.COLOR_MATERIAL, width=2)
        
        # Label
        try:
            font = ImageFont.truetype("/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf", 14)
        except (OSError, IOError):
            font = ImageFont.load_default()
        
        shape = material.get("shape", "Custom")
        label = f"{shape} ({width_mm:.1f}×{height_mm:.1f} mm)"
        draw.text((x1 + 10, y1 + height_px - 30), label, 
                 fill=PreviewService.COLOR_MATERIAL, font=font)
    
    @staticmethod
    def _draw_dashed_rect(draw: ImageDraw.Draw, coords: List[int], color: str, width: int = 2):
        """Draw dashed rectangle"""
        x1, y1, x2, y2 = coords
        dash_length = 10
        gap_length = 5
        
        # Top
        x = x1
        while x < x2:
            draw.line([(x, y1), (min(x + dash_length, x2), y1)], fill=color, width=width)
            x += dash_length + gap_length
        
        # Right
        y = y1
        while y < y2:
            draw.line([(x2, y), (x2, min(y + dash_length, y2))], fill=color, width=width)
            y += dash_length + gap_length
        
        # Bottom
        x = x2
        while x > x1:
            draw.line([(x, y2), (max(x - dash_length, x1), y2)], fill=color, width=width)
            x -= dash_length + gap_length
        
        # Left
        y = y2
        while y > y1:
            draw.line([(x1, y), (x1, max(y - dash_length, y1))], fill=color, width=width)
            y -= dash_length + gap_length
    
    @staticmethod
    def _draw_image(canvas: Image.Image, upload_stage: dict, layout: dict, 
                   offset_x: int, offset_y: int):
        """Draw image content at calculated position using layout offsets"""
        upload_data = upload_stage.get("data", {})
        
        # OPTION 2: Try base64 first (primary), fallback to file path
        image_base64 = upload_data.get("image_base64")
        if image_base64:
            # Decode base64 data URL
            import base64
            import io
            if "," in image_base64:
                base64_data = image_base64.split(",", 1)[1]
            else:
                base64_data = image_base64
            try:
                image_bytes = base64.b64decode(base64_data)
                img = Image.open(io.BytesIO(image_bytes))
            except Exception as e:
                logger.error(f"Base64 decode failed: {e}")
                return
        else:
            # Fallback: read from file (backward compatibility)
            image_path = upload_data.get("image_path")
            if not image_path or not Path(image_path).exists():
                logger.warning(f"No image data (base64 or file): {image_path}")
                return
            try:
                img = Image.open(image_path)
            except Exception as e:
                logger.error(f"File read failed: {e}")
                return
        
        # Continue with image positioning (common for both paths)
        img_width, img_height = img.size
        
        # Get layout offsets (relative positioning)
        img_on_mat = layout.get("image_on_material", {})
        mat_on_burn = layout.get("material_on_burn_area", {})
        
        # Material position on burn area (mm)
        mat_center_x_mm = mat_on_burn.get("center_x_mm", K6Constants.BURN_WIDTH_MM / 2)
        mat_center_y_mm = mat_on_burn.get("center_y_mm", K6Constants.BURN_HEIGHT_MM / 2)
        
        # Image position on material (mm, material-relative coords)
        img_offset_x_mm = img_on_mat.get("center_x_mm", 0)  # 0 = centered on material
        img_offset_y_mm = img_on_mat.get("center_y_mm", 0)
        
        # Calculate final image position on burn area (px)
        # Material center in burn area coordinates
        mat_center_x_px = int(mat_center_x_mm / K6Constants.RESOLUTION_MM_PX)
        mat_center_y_px = int(mat_center_y_mm / K6Constants.RESOLUTION_MM_PX)
        
        # Image offset from material center
        img_offset_x_px = int(img_offset_x_mm / K6Constants.RESOLUTION_MM_PX)
        img_offset_y_px = int(img_offset_y_mm / K6Constants.RESOLUTION_MM_PX)
        
        # Final image center in burn area coordinates
        img_center_x_px = mat_center_x_px + img_offset_x_px
        img_center_y_px = mat_center_y_px + img_offset_y_px
        
        # Calculate paste position (top-left)
        paste_x = offset_x + img_center_x_px - (img_width // 2)
        paste_y = offset_y + img_center_y_px - (img_height // 2)
        
        # DO NOT resize - preview must show actual size with crop indicators
        # Burn validation happens at burn time, not preview time
        
        # Convert to RGBA for transparency support
        if img.mode != "RGBA":
            img = img.convert("RGBA")
        
        # Paste image
        canvas.paste(img, (paste_x, paste_y), img)
        
        # Draw green dashed border around image
        border_coords = [
            paste_x,
            paste_y,
            paste_x + img_width,
            paste_y + img_height
        ]
        draw = ImageDraw.Draw(canvas)
        PreviewService._draw_dashed_rect(
            draw,
            border_coords,
            PreviewService.COLOR_IMAGE,
            width=2
        )
    
    @staticmethod
    def _detect_crops(job: dict) -> List[dict]:
        """Detect regions that will be cropped during burn"""
        crop_regions = []
        
        # Check if image exceeds burn area
        upload_stage = job.get("stages", {}).get("upload")
        if upload_stage:
            img_data = upload_stage.get("data", {})
            img_width = img_data.get("width", 0)
            img_height = img_data.get("height", 0)
            
            if img_width > K6Constants.BURN_WIDTH_PX:
                crop_regions.append({
                    "type": "image_right",
                    "amount_px": img_width - K6Constants.BURN_WIDTH_PX,
                    "amount_mm": (img_width - K6Constants.BURN_WIDTH_PX) * K6Constants.RESOLUTION_MM_PX
                })
            
            if img_height > K6Constants.BURN_HEIGHT_PX:
                crop_regions.append({
                    "type": "image_bottom",
                    "amount_px": img_height - K6Constants.BURN_HEIGHT_PX,
                    "amount_mm": (img_height - K6Constants.BURN_HEIGHT_PX) * K6Constants.RESOLUTION_MM_PX
                })
        
        # Check if material exceeds burn area
        material = job.get("material", {}).get("target")
        if material:
            width_mm = material.get("width_mm", 0)
            height_mm = material.get("height_mm", 0)
            
            if width_mm > K6Constants.BURN_WIDTH_MM:
                crop_regions.append({
                    "type": "material_right",
                    "amount_mm": width_mm - K6Constants.BURN_WIDTH_MM
                })
            
            if height_mm > K6Constants.BURN_HEIGHT_MM:
                crop_regions.append({
                    "type": "material_bottom",
                    "amount_mm": height_mm - K6Constants.BURN_HEIGHT_MM
                })
        
        return crop_regions
    
    @staticmethod
    def _draw_crop_indicator(draw: ImageDraw.Draw, region: dict, offset_x: int, offset_y: int):
        """Draw red diagonal stripes over cropped region"""
        crop_type = region.get("type")
        
        if crop_type == "image_bottom":
            # Bottom edge crop (most common: 1600×1600 on 1600×1520)
            x1 = offset_x
            y1 = offset_y + K6Constants.BURN_HEIGHT_PX
            x2 = offset_x + K6Constants.BURN_WIDTH_PX
            # Extend down to show cropped region
            amount_px = region.get("amount_px", 100)
            y2 = y1 + amount_px
            
            # Red fill for cropped area
            draw.rectangle([x1, y1, x2, y2], fill=PreviewService.COLOR_CROP, outline=PreviewService.COLOR_CROP, width=2)
            
        elif crop_type == "image_right":
            # Right edge crop
            x1 = offset_x + K6Constants.BURN_WIDTH_PX
            y1 = offset_y
            amount_px = region.get("amount_px", 100)
            x2 = x1 + amount_px
            y2 = offset_y + K6Constants.BURN_HEIGHT_PX
            
            # Red fill for cropped area
            draw.rectangle([x1, y1, x2, y2], fill=PreviewService.COLOR_CROP, outline=PreviewService.COLOR_CROP, width=2)
            
        elif crop_type == "material_right":
            # Right edge crop
            x1 = offset_x + K6Constants.BURN_WIDTH_PX
            y1 = offset_y
            x2 = offset_x + PreviewService.CANVAS_WIDTH
            y2 = offset_y + K6Constants.BURN_HEIGHT_PX
            
            # Diagonal stripes
            for i in range(0, K6Constants.BURN_HEIGHT_PX, 20):
                draw.line([(x1, y1 + i), (x2, y1 + i + (x2 - x1))], 
                         fill=PreviewService.COLOR_CROP, width=2)
        
        elif crop_type == "material_bottom":
            # Bottom edge crop
            x1 = offset_x
            y1 = offset_y + K6Constants.BURN_HEIGHT_PX
            x2 = offset_x + K6Constants.BURN_WIDTH_PX
            y2 = offset_y + PreviewService.CANVAS_HEIGHT
            
            # Diagonal stripes
            for i in range(0, K6Constants.BURN_WIDTH_PX, 20):
                draw.line([(x1 + i, y1), (x1 + i + (y2 - y1), y2)], 
                         fill=PreviewService.COLOR_CROP, width=2)
    
    @staticmethod
    def _generate_annotations(job: dict) -> List[dict]:
        """Generate annotation text for preview"""
        annotations = []
        
        # Image dimensions
        upload_stage = job.get("stages", {}).get("upload")
        if upload_stage:
            data = upload_stage.get("data", {})
            width = data.get("width", 0)
            height = data.get("height", 0)
            width_mm = width * K6Constants.RESOLUTION_MM_PX
            height_mm = height * K6Constants.RESOLUTION_MM_PX
            
            annotations.append({
                "text": f"✓ Image: {width}×{height} px ({width_mm:.1f}×{height_mm:.1f} mm)",
                "color": PreviewService.COLOR_SUCCESS
            })
        
        # Material info
        material = job.get("material", {}).get("target")
        if material:
            shape = material.get("shape", "Custom")
            width_mm = material.get("width_mm", 0)
            height_mm = material.get("height_mm", 0)
            
            annotations.append({
                "text": f"✓ Material: {shape} ({width_mm:.1f}×{height_mm:.1f} mm)",
                "color": PreviewService.COLOR_SUCCESS
            })
        
        # Warnings
        crop_regions = PreviewService._detect_crops(job)
        for region in crop_regions:
            crop_type = region.get("type")
            amount_mm = region.get("amount_mm", 0)
            direction = "right" if "right" in crop_type else "bottom"
            
            # Distinguish between image and material crops
            if crop_type.startswith("image_"):
                subject = "Image"
            elif crop_type.startswith("material_"):
                subject = "Material"
            else:
                subject = "Content"
            
            annotations.append({
                "text": f"⚠ {subject} exceeds burn area by {amount_mm:.1f}mm ({direction})",
                "color": PreviewService.COLOR_WARNING
            })
        
        # Status
        if not crop_regions:
            annotations.append({
                "text": "✓ Ready to burn",
                "color": PreviewService.COLOR_SUCCESS
            })
        
        return annotations
    
    @staticmethod
    def _draw_annotations(draw: ImageDraw.Draw, annotations: List[dict]):
        """Draw annotation text on preview"""
        try:
            font = ImageFont.truetype("/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf", 16)
        except (OSError, IOError):
            font = ImageFont.load_default()
        
        # Position annotations in top-right corner
        x = PreviewService.CANVAS_WIDTH - 400
        y = 20
        line_height = 25
        
        for annotation in annotations:
            text = annotation.get("text", "")
            color = annotation.get("color", PreviewService.COLOR_ANNOTATION)
            
            # Background box
            bbox = draw.textbbox((x, y), text, font=font)
            draw.rectangle([bbox[0] - 5, bbox[1] - 2, bbox[2] + 5, bbox[3] + 2], 
                          fill="#FFFFFF", outline="#CCCCCC")
            
            # Text
            draw.text((x, y), text, fill=color, font=font)
            y += line_height
    
    @staticmethod
    def detect_warnings(job: dict) -> List[str]:
        """Generate warning messages for job"""
        warnings = []
        
        crop_regions = PreviewService._detect_crops(job)
        for region in crop_regions:
            crop_type = region.get("type")
            amount_mm = region.get("amount_mm", 0)
            direction = "right" if "right" in crop_type else "bottom"
            subject = "Image" if str(crop_type).startswith("image_") else "Material"
            warnings.append(f"{subject} exceeds burn area by {amount_mm:.1f}mm ({direction})")
        
        return warnings
