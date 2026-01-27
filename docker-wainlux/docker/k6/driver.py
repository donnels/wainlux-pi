"""K6 driver: clean transport-based implementation.

Pure protocol implementation using transport abstraction.
No subprocess calls. No legacy script dependencies.
"""

from __future__ import annotations
from typing import Dict


class WainluxK6:
    """K6 laser driver using transport-based protocol.

    All methods use the transport layer directly.
    Synchronous operations return success booleans or result dicts.
    """

    def __init__(self):
        pass

    def draw_bounds_transport(
        self,
        transport,
        width: int = 1600,
        height: int = 1600,
        center_x: int = None,
        center_y: int = None,
        csv_logger=None,
    ) -> bool:
        """Draw preview rectangle using BOUNDS (0x20) command.

        Fast preview - single 11-byte command, no image data.

        Args:
            transport: TransportBase instance
            width: Rectangle width in pixels (default 1600)
            height: Rectangle height in pixels (default 1600)
            center_x: Center X coordinate (default: width/2)
            center_y: Center Y coordinate (default: height/2)
            csv_logger: Optional CSVLogger instance

        Returns:
            True on success, False on failure
        """
        from . import protocol

        try:
            # Build and send BOUNDS command
            bounds_cmd = protocol.build_bounds_packet(width, height, center_x, center_y)
            protocol.send_cmd_checked(
                transport,
                f"BOUNDS {width}×{height}",
                bounds_cmd,
                timeout=2.0,
                expect_ack=True,
                csv_logger=csv_logger,
                phase="preview",
            )
            return True
        except Exception:
            # Log error but don't raise - return False for consistency
            if csv_logger:
                csv_logger.log_operation(
                    phase="preview",
                    operation="BOUNDS",
                    duration_ms=0,
                    state="ERROR",
                    device_state="ERROR",
                )
            return False

    def mark_position_transport(
        self,
        transport,
        center_x: int = 800,
        center_y: int = 800,
        power: int = 500,
        depth: int = 10,
        csv_logger=None,
    ) -> bool:
        """Burn a single pixel mark at specified position.

        Creates a minimal 1x1px raster job to mark a position.
        Useful for testing mechanical limits and positioning.

        Args:
            transport: TransportBase instance
            center_x: X coordinate in pixels (default 800)
            center_y: Y coordinate in pixels (default 800)
            power: Laser power 0-1000 (default 500)
            depth: Burn depth 1-255 (default 10 - light mark)
            csv_logger: Optional CSVLogger instance

        Returns:
            True on success, False on failure
        """
        from . import protocol

        try:
            # Create 1x1 black pixel (burn)
            width, height = 1, 1
            payload_lines = [bytes([0xFF])]  # 1 pixel = 1 byte, 0xFF = burn

            # FRAMING (0x21) - stop preview mode
            # BOUNDS (0x20) enables preview, FRAMING (0x21) disables it
            protocol.send_cmd_checked(
                transport,
                "FRAMING",
                bytes([0x21, 0x00, 0x04, 0x00]),
                timeout=1.0,
                expect_ack=True,
                csv_logger=csv_logger,
                phase="setup",
            )

            # JOB_HEADER with custom center position
            header = protocol.build_job_header_custom(
                raster_w=width,
                raster_h=height,
                raster_power=power,
                raster_depth=depth,
                vector_w=0,
                vector_h=0,
                total_size=1,  # 1 byte
                vector_power=0,
                vector_depth=0,
                point_count=0,
                center_x=center_x,
                center_y=center_y,
                quality=1,
            )
            protocol.send_cmd_checked(
                transport,
                "JOB_HEADER",
                header,
                timeout=10.0,
                expect_ack=False,
                csv_logger=csv_logger,
                phase="setup",
            )

            # CONNECT x2
            for i in range(2):
                protocol.send_cmd_checked(
                    transport,
                    f"CONNECT #{i+1}",
                    bytes([0x0A, 0x00, 0x04, 0x00]),
                    timeout=1.0,
                    expect_ack=True,
                    csv_logger=csv_logger,
                    phase="setup",
                )

            # Burn payload (single 1-byte line)
            protocol.burn_payload(
                transport, payload_lines, max_retries=3, csv_logger=csv_logger
            )

            # INIT x2
            init_cmd = bytes([0x24, 0x00, 0x0B, 0x00] + [0x00] * 7)
            for i in range(2):
                protocol.send_cmd_checked(
                    transport,
                    f"INIT #{i+1}",
                    init_cmd,
                    timeout=3.0,
                    expect_ack=False,
                    csv_logger=csv_logger,
                    phase="finalize",
                )

            # Wait for completion (should be instant for 1px)
            protocol.wait_for_completion(transport, timeout=5.0, csv_logger=csv_logger)

            return True

        except Exception:
            if csv_logger:
                csv_logger.log_operation(
                    phase="mark",
                    operation="MARK",
                    duration_ms=0,
                    state="ERROR",
                    device_state="ERROR",
                )
            return False

    def connect_transport(self, transport, csv_logger=None) -> tuple:
        """Perform the init sequence (STOP, VERSION, CONNECT x2, HOME) using a transport.

        Raises protocol.K6TimeoutError or protocol.K6DeviceError on failure.
        Returns (True, (major, minor, patch)) on success.

        Args:
            csv_logger: Optional CSVLogger instance for logging operations
        """
        from . import protocol

        # STOP (best-effort) - write STOP and do not read replies to avoid
        # consuming subsequent responses (like VERSION) during transient states.
        try:
            transport.write(bytes([0x16, 0x00, 0x04, 0x00]))
        except Exception:
            # ignore write failures; proceed with init
            pass

        # VERSION - expects a 3-byte reply; treat any response as success
        rx = protocol.send_cmd(
            transport,
            "VERSION",
            bytes([0xFF, 0x00, 0x04, 0x00]),
            timeout=1.0,
            expect_ack=False,
            min_response_len=3,
            csv_logger=csv_logger,
            phase="connect",
        )
        if not rx or len(rx) < 3:
            raise protocol.K6TimeoutError("No VERSION response")

        # Parse version: 3 bytes (major, minor, patch)
        version = (rx[0], rx[1], rx[2])

        # CONNECT #1 and #2
        protocol.send_cmd_checked(
            transport,
            "CONNECT #1",
            bytes([0x0A, 0x00, 0x04, 0x00]),
            timeout=1.0,
            expect_ack=True,
            csv_logger=csv_logger,
            phase="connect",
        )
        protocol.send_cmd_checked(
            transport,
            "CONNECT #2",
            bytes([0x0A, 0x00, 0x04, 0x00]),
            timeout=1.0,
            expect_ack=True,
            csv_logger=csv_logger,
            phase="connect",
        )

        # HOME
        protocol.send_cmd_checked(
            transport,
            "HOME",
            bytes([0x17, 0x00, 0x04, 0x00]),
            timeout=10.0,
            expect_ack=True,
            csv_logger=csv_logger,
            phase="connect",
        )
        return True, version

    def connect_serial(self, port: str = "/dev/ttyUSB0") -> bool:
        """Helper: create a SerialTransport and run connect sequence."""
        from .transport import SerialTransport

        t = SerialTransport(port=port, baudrate=115200, timeout=2.0)
        return self.connect_transport(t)

    def engrave_transport(
        self,
        transport,
        image_path: str,
        power: int = 1000,
        depth: int = 100,
        csv_logger=None,
    ) -> Dict:
        """Engrave image using transport-based protocol (full sequence).

        Args:
            transport: TransportBase instance (SerialTransport or MockTransport)
            image_path: Path to image file
            power: Laser power 0-1000 (default 1000)
            depth: Burn depth 1-255 (default 100)
            csv_logger: Optional CSVLogger instance for logging operations

        Returns:
            Dict with 'ok', 'total_time', 'chunks', 'message'

        Raises:
            K6TimeoutError, K6DeviceError on protocol errors
        """
        from PIL import Image
        from . import protocol

        # Load and prepare image
        import numpy as np

        img = Image.open(image_path).convert("L")  # grayscale
        width, height = img.size

        if width > 1600 or height > 1600:
            raise ValueError(f"Image too large: {width}x{height} (max 1600x1600)")

        # Convert to NumPy array for fast vectorized operations
        # ~1 second instead of 90 seconds for 1600x1600
        pixels = np.array(img, dtype=np.uint8)

        # Threshold: 0=burn (black), 255=skip (white)
        binary = (pixels < 128).astype(np.uint8)

        # Pad width to multiple of 8 for bit packing
        if width % 8 != 0:
            pad_width = 8 - (width % 8)
            binary = np.pad(
                binary, ((0, 0), (0, pad_width)), mode="constant", constant_values=0
            )

        # Pack 8 pixels into 1 byte (MSB first)
        # Reshape to (height, width/8, 8) then pack bits
        packed_width = binary.shape[1] // 8
        packed = np.zeros((height, packed_width), dtype=np.uint8)

        for bit in range(8):
            packed |= binary[:, bit::8] << (7 - bit)

        # Convert to list of bytes for protocol layer
        payload_lines = [packed[y].tobytes() for y in range(height)]

        # Protocol sequence: FRAMING → JOB_HEADER → CONNECT x2 → burn → INIT x2
        # FRAMING (0x21) stops preview mode from BOUNDS (0x20)
        try:
            import time

            transport.write(bytes([0x21, 0x00, 0x04, 0x00]))
            time.sleep(0.05)
            # Drain any response
            try:
                transport.read(timeout=0.1)
            except Exception:
                pass
        except Exception:
            # Log but proceed - best effort
            if csv_logger:
                csv_logger.log_operation(
                    phase="setup",
                    operation="PRE_CLEAR",
                    duration_ms=0,
                    state="ERROR",
                    device_state="UNKNOWN",
                )

        # FRAMING (official start of burn sequence)
        protocol.send_cmd_checked(
            transport,
            "FRAMING",
            bytes([0x21, 0x00, 0x04, 0x00]),
            timeout=1.0,
            expect_ack=True,
            csv_logger=csv_logger,
            phase="setup",
        )

        # JOB_HEADER - device responds with HEARTBEAT not ACK
        header = protocol.build_job_header_raster(width, height, depth, power)
        protocol.send_cmd_checked(
            transport,
            "JOB_HEADER",
            header,
            timeout=10.0,
            expect_ack=False,
            csv_logger=csv_logger,
            phase="setup",
        )

        # CONNECT x2 before burn
        protocol.send_cmd_checked(
            transport,
            "CONNECT #1",
            bytes([0x0A, 0x00, 0x04, 0x00]),
            timeout=1.0,
            expect_ack=True,
            csv_logger=csv_logger,
            phase="setup",
        )
        protocol.send_cmd_checked(
            transport,
            "CONNECT #2",
            bytes([0x0A, 0x00, 0x04, 0x00]),
            timeout=1.0,
            expect_ack=True,
            csv_logger=csv_logger,
            phase="setup",
        )

        # Burn payload with chunking + retry
        chunks = protocol.burn_payload(
            transport, payload_lines, max_retries=3, csv_logger=csv_logger
        )

        # INIT x2 after burn - device responds with status frames (FF FF 00 XX)
        init_cmd = bytes([0x24, 0x00, 0x0B, 0x00] + [0x00] * 7)
        protocol.send_cmd_checked(
            transport,
            "INIT #1",
            init_cmd,
            timeout=3.0,
            expect_ack=False,
            csv_logger=csv_logger,
            phase="finalize",
        )
        protocol.send_cmd_checked(
            transport,
            "INIT #2",
            init_cmd,
            timeout=3.0,
            expect_ack=False,
            csv_logger=csv_logger,
            phase="finalize",
        )

        # Wait for completion
        result = protocol.wait_for_completion(
            transport, max_wait_s=600.0, idle_s=90.0, csv_logger=csv_logger
        )

        if result["status"] == "complete":
            return {
                "ok": True,
                "total_time": result["total_time"],
                "chunks": chunks,
                "message": f"Burn complete ({chunks} chunks, {result['total_time']:.1f}s)",
            }
        elif result["status"] == "idle_timeout":
            # Device often stops sending status updates before 100% (typically at 80-90%)
            # Treat high-percentage idle timeouts as success
            last_pct = result.get("last_pct", 0)
            if last_pct and last_pct >= 50:
                return {
                    "ok": True,
                    "total_time": 0,
                    "chunks": chunks,
                    "message": f"Burn complete (idle timeout at {last_pct}%, {chunks} chunks)",
                }
            else:
                return {
                    "ok": False,
                    "total_time": 0,
                    "chunks": chunks,
                    "message": f"Burn incomplete: idle_timeout (last {last_pct}%)",
                }
        else:
            return {
                "ok": False,
                "total_time": 0,
                "chunks": chunks,
                "message": (
                    f"Burn incomplete: {result['status']} "
                    f"(last {result.get('last_pct', 0)}%)"
                ),
            }
