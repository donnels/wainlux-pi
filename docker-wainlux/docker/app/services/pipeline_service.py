"""Pipeline service: Unix-style data processing pipeline

Each function is independent, file-based, inspectable:
- process_image: upload.png → processed.npy + preview.png
- build_commands: processed.npy → commands.bin + metadata.json
- execute_commands: commands.bin → logs + byte dumps

Like Unix pipes: each step reads files, writes files, can be tested alone.
"""

from pathlib import Path
from typing import Dict, Optional
import json
import numpy as np
from PIL import Image

from k6.commands import K6CommandBuilder, CommandSequence
from utils.timestamps import file_timestamp, iso_timestamp


class PipelineService:
    """File-based pipeline for K6 laser workflow"""

    @staticmethod
    def process_image(
        input_path: str,
        output_dir: Path,
        threshold: int = 128,
        invert: bool = False
    ) -> Dict:
        """Process image to burn-ready format (Step 2).

        Input:  upload.png (any format)
        Output: processed.npy (packed bits) + preview.png (1-bit)
        Expose: Threshold, invert, pixel counts, dimensions

        Args:
            input_path: Path to uploaded image
            output_dir: Directory for output files
            threshold: Pixel threshold 0-255 (default 128)
                      pixels < threshold = black (burn)
                      pixels >= threshold = white (skip)
            invert: If True, invert black/white (burn white areas instead)

        Returns:
            {
                "processed_path": "processed_TIMESTAMP.npy",
                "preview_path": "processed_TIMESTAMP.png",
                "width": 1500,
                "height": 1080,
                "stats": {
                    "black_pixels": 456789,
                    "white_pixels": 123456,
                    "burn_percentage": 78.5,
                    "threshold": 128,
                    "inverted": false,
                    "original_width": 1500,
                    "padded_width": 1504
                }
            }
        """
        # Validate threshold
        threshold = max(0, min(255, threshold))

        # Load and convert to grayscale
        img = Image.open(input_path).convert("L")
        width, height = img.size

        # Validate size
        if width > 1600 or height > 1600:
            raise ValueError(f"Image too large: {width}×{height} (max 1600×1600)")

        # Convert to NumPy for fast processing
        pixels = np.array(img, dtype=np.uint8)

        # Threshold: 0=burn (black), 1=skip (white)
        binary = (pixels >= threshold).astype(np.uint8)
        
        # Invert if requested (swap 0↔1)
        if invert:
            binary = 1 - binary

        # Count pixels
        black_pixels = int(np.sum(binary == 0))
        white_pixels = int(np.sum(binary == 1))

        # Pad width to 8px boundary for bit packing
        original_width = width
        if width % 8 != 0:
            pad_width = 8 - (width % 8)
            binary = np.pad(
                binary,
                ((0, 0), (0, pad_width)),
                mode="constant",
                constant_values=1  # white (skip)
            )
            width = binary.shape[1]

        # Pack 8 pixels per byte (MSB first)
        packed_width = width // 8
        packed = np.zeros((height, packed_width), dtype=np.uint8)
        for bit in range(8):
            packed |= binary[:, bit::8] << (7 - bit)

        # Generate timestamp
        timestamp = file_timestamp()

        # Save processed data
        processed_path = output_dir / f"processed_{timestamp}.npy"
        np.save(processed_path, packed)

        # Generate preview (1-bit visualization)
        preview_img = Image.fromarray((binary * 255).astype(np.uint8), mode="L")
        preview_path = output_dir / f"processed_{timestamp}.png"
        preview_img.save(preview_path)

        # Stats
        total_pixels = black_pixels + white_pixels
        burn_pct = (black_pixels / total_pixels * 100) if total_pixels > 0 else 0

        return {
            "processed_path": str(processed_path),
            "preview_path": str(preview_path),
            "width": original_width,
            "height": height,
            "stats": {
                "black_pixels": black_pixels,
                "white_pixels": white_pixels,
                "burn_percentage": round(burn_pct, 2),
                "threshold": threshold,
                "inverted": invert,
                "original_width": original_width,
                "padded_width": width
            }
        }

    @staticmethod
    def build_commands(
        processed_path: str,
        power: int,
        depth: int,
        center_x: int,
        center_y: int,
        output_dir: Path
    ) -> Dict:
        """Build command sequence from processed data (Step 3).

        Input:  processed.npy (packed bits)
        Output: commands.bin (protocol stream) + metadata.json
        Expose: Command count, byte size, timing, bounds

        Args:
            processed_path: Path to .npy file from process_image()
            power: Laser power 0-1000
            depth: Burn depth 1-255
            center_x: Center X position (pixels)
            center_y: Center Y position (pixels)
            output_dir: Directory for output files

        Returns:
            {
                "command_path": "commands_TIMESTAMP.bin",
                "metadata_path": "commands_TIMESTAMP.json",
                "sequence": {
                    "total_commands": 1084,
                    "data_chunks": 1080,
                    "total_bytes": 216450,
                    "estimated_time_sec": 245.7
                },
                "bounds": {"min_x": 67, "max_x": 1567, ...},
                "validation": {"within_limits": true, "warnings": []}
            }
        """
        # Validate parameters
        power = max(0, min(1000, power))
        depth = max(1, min(255, depth))

        # Load processed data
        if not Path(processed_path).exists():
            raise FileNotFoundError(f"Processed file not found: {processed_path}")

        packed = np.load(processed_path)
        height, packed_width = packed.shape
        width = packed_width * 8

        # Build command sequence
        seq = CommandSequence(description=f"Burn {width}×{height}px")

        # Setup phase
        seq.add(K6CommandBuilder.build_framing())
        seq.add(K6CommandBuilder.build_job_header(
            width=width, height=height, depth=depth, power=power,
            center_x=center_x, center_y=center_y
        ))
        seq.add(K6CommandBuilder.build_connect(1))
        seq.add(K6CommandBuilder.build_connect(2))

        # Data chunks
        for line_num in range(height):
            line_data = packed[line_num].tobytes()
            seq.add(K6CommandBuilder.build_data_packet(line_data, line_num))

        # Finalization
        seq.add(K6CommandBuilder.build_init(1))
        seq.add(K6CommandBuilder.build_init(2))
        seq.add(K6CommandBuilder.build_connect(1))
        seq.add(K6CommandBuilder.build_connect(2))

        # Get stats
        sequence_stats = seq.stats
        bounds = seq.get_bounds()

        # Validate bounds
        warnings = []
        if bounds['max_x'] > 1600:
            warnings.append(f"X exceeds limit: {bounds['max_x']} > 1600")
        if bounds['max_y'] > 1520:
            warnings.append(f"Y exceeds limit: {bounds['max_y']} > 1520")

        within_limits = len(warnings) == 0

        # Generate timestamp
        timestamp = file_timestamp()

        # Serialize commands to binary
        command_path = output_dir / f"commands_{timestamp}.bin"
        with open(command_path, 'wb') as f:
            for cmd in seq.commands:
                f.write(cmd.to_bytes())

        # Save metadata
        metadata = {
            "timestamp": iso_timestamp(),
            "source": {
                "processed_file": processed_path,
                "width": width,
                "height": height
            },
            "parameters": {
                "power": power,
                "depth": depth,
                "center_x": center_x,
                "center_y": center_y
            },
            "sequence": sequence_stats,
            "bounds": bounds,
            "validation": {
                "within_limits": within_limits,
                "warnings": warnings
            }
        }

        metadata_path = output_dir / f"commands_{timestamp}.json"
        with open(metadata_path, 'w') as f:
            json.dump(metadata, f, indent=2)

        return {
            "command_path": str(command_path),
            "metadata_path": str(metadata_path),
            "sequence": sequence_stats,
            "bounds": bounds,
            "validation": {
                "within_limits": within_limits,
                "warnings": warnings
            }
        }
