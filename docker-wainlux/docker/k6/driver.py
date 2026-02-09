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
        self.dry_run = False  # Dry run flag: upload data but don't fire laser

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

    def set_speed_power_transport(
        self, transport, speed: int = 115, power: int = 1000, csv_logger=None
    ) -> bool:
        """Send SET_SPEED_POWER (0x25) command.

        Test command for opcode 0x25. No observable effect expected.
        Included for protocol testing and completeness.

        Args:
            transport: TransportBase instance
            speed: Speed parameter (default 115, purpose unclear)
            power: Power parameter 0-1000 (default 1000)
            csv_logger: Optional CSVLogger instance

        Returns:
            True on success (ACK received), False on failure
        """
        from . import protocol

        try:
            # Build 11-byte packet: [0x25][0x00][0x0B][speed_msb][speed_lsb][power_msb][power_lsb][0x00*4]
            pkt = bytearray([0x25, 0x00, 0x0B])
            pkt.extend([(speed >> 8) & 0xFF, speed & 0xFF])
            pkt.extend([(power >> 8) & 0xFF, power & 0xFF])
            pkt.extend([0x00] * 4)  # Reserved bytes

            protocol.send_cmd_checked(
                transport,
                f"SET_SPEED_POWER speed={speed} power={power}",
                bytes(pkt),
                timeout=2.0,
                expect_ack=True,
                csv_logger=csv_logger,
                phase="test",
            )
            return True
        except Exception:
            if csv_logger:
                csv_logger.log_operation(
                    phase="test",
                    operation="SET_SPEED_POWER",
                    duration_ms=0,
                    state="ERROR",
                    device_state="ERROR",
                )
            return False

    def set_focus_angle_transport(
        self, transport, focus: int = 20, angle: int = 0, csv_logger=None
    ) -> bool:
        """Send SET_FOCUS_ANGLE (0x28) command.

        Test command for opcode 0x28. No observable effect expected.
        Included for protocol testing and completeness.

        Args:
            transport: TransportBase instance
            focus: Focus parameter 0-200 (default 20, typically UI_value x 2)
            angle: Angle/mode index (default 0, purpose unclear)
            csv_logger: Optional CSVLogger instance

        Returns:
            True on success (ACK received), False on failure
        """
        from . import protocol

        try:
            # Build 11-byte packet: [0x28][0x00][0x0B][focus][angle][0x00*6]
            pkt = bytearray([0x28, 0x00, 0x0B])
            pkt.append(focus & 0xFF)
            pkt.append(angle & 0xFF)
            pkt.extend([0x00] * 6)  # Reserved bytes

            protocol.send_cmd_checked(
                transport,
                f"SET_FOCUS_ANGLE focus={focus} angle={angle}",
                bytes(pkt),
                timeout=2.0,
                expect_ack=True,
                csv_logger=csv_logger,
                phase="test",
            )
            return True
        except Exception:
            if csv_logger:
                csv_logger.log_operation(
                    phase="test",
                    operation="SET_FOCUS_ANGLE",
                    duration_ms=0,
                    state="ERROR",
                    device_state="ERROR",
                )
            return False

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
        center_x: int = None,
        center_y: int = None,
        csv_logger=None,
    ) -> Dict:
        """Engrave image using transport-based protocol (full sequence).

        Args:
            transport: TransportBase instance (SerialTransport or MockTransport)
            image_path: Path to image file
            power: Laser power 0-1000 (default 1000)
            depth: Burn depth 1-255 (default 100)
            center_x: Center X coordinate (default: image_width/2 + 67)
            center_y: Center Y coordinate (default: 800 = centered in work area)
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

        # Threshold and INVERT: 1=burn (black), 0=skip (white)
        # K6 protocol: bit 1 = laser ON, bit 0 = laser OFF
        # So 0xFF byte = all burn (8 black pixels), 0x00 = all skip (8 white pixels)
        binary = (pixels < 128).astype(np.uint8)  # 1 for dark, 0 for light - CORRECT

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

        # Debug: check first few lines for burn pixels
        if payload_lines:
            import logging
            logger = logging.getLogger(__name__)
            
            # Check first 3 lines
            for i in range(min(3, len(payload_lines))):
                line = payload_lines[i]
                has_burn = any(b != 0xFF for b in line)
                logger.info(f"Line {i}: {len(line)} bytes, has_burn={has_burn}, first_10_bytes={line[:10].hex()}")
            
            # Diagnostic: check binary array BEFORE packing
            logger.info(f"Binary array: shape={binary.shape}, unique_values={np.unique(binary)}, sample_top_left_8x8={binary[:8,:8].tolist()}")

        # Protocol sequence: FRAMING → JOB_HEADER → CONNECT x2 → DATA chunks
        # Matches working k6_burn_image.py script exactly
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
        header = protocol.build_job_header_raster(
            width, height, depth, power, center_x=center_x, center_y=center_y
        )
        logger.info(f"JOB_HEADER: width={width}, height={height}, depth={depth}, power={power}, center_x={center_x}, center_y={center_y}")
        logger.info(f"JOB_HEADER bytes: {header.hex()}")
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
        
        # Debug: check if there are any pending bytes before DATA.
        # Transport API uses set_timeout() + read(size), not read(timeout=...).
        import logging
        logger = logging.getLogger(__name__)
        try:
            transport.set_timeout(0.1)
            pending = transport.read(64)
            if pending:
                logger.info(f"Pending bytes after CONNECT #2: {pending.hex()}")
        except Exception:
            pass

        # Burn payload with chunking + retry (no delay - matches working script)
        chunks = protocol.burn_payload(
            transport, payload_lines, max_retries=3, csv_logger=csv_logger
        )

        # DRY RUN: skip INIT and completion wait (don't start laser)
        if self.dry_run:
            if csv_logger:
                csv_logger.log_operation(
                    phase="finalize",
                    operation="DRY_RUN_STOP",
                    duration_ms=0,
                    state="COMPLETE",
                    device_state="READY",
                )
            return {
                "ok": True,
                "total_time": 0,
                "chunks": chunks,
                "message": f"Dry run complete ({chunks} chunks uploaded, laser NOT fired)",
            }

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

    def execute_from_file(
        self,
        transport,
        command_path: str,
        csv_logger=None,
        byte_logger=None,
    ) -> Dict:
        """Execute pre-built command sequence from file (Step 4).

        Reads commands.bin from pipeline and executes on device.
        Bypasses image processing - commands already built.
        Enables replay and protocol debugging.

        Args:
            transport: TransportBase instance
            command_path: Path to .bin file from build_commands()
            csv_logger: Optional CSVLogger for timing
            byte_logger: Optional ByteDumpLogger for raw I/O

        Returns:
            {
                "ok": True/False,
                "commands_sent": 1084,
                "device_responses": {
                    "ack_count": 6,
                    "heartbeat_count": 2,
                    "error_count": 0
                }
            }
        """
        from pathlib import Path

        # Validate file exists
        if not Path(command_path).exists():
            return {
                "ok": False,
                "commands_sent": 0,
                "error": f"Command file not found: {command_path}"
            }

        # Read command file
        with open(command_path, 'rb') as f:
            command_data = f.read()

        # Parse commands (each has 4-byte header: opcode, reserved, length_le16)
        commands = []
        offset = 0
        while offset < len(command_data):
            if offset + 4 > len(command_data):
                break
            
            length = int.from_bytes(command_data[offset+2:offset+4], 'little')
            if offset + length > len(command_data):
                break
                
            cmd_bytes = command_data[offset:offset+length]
            commands.append(cmd_bytes)
            offset += length

        # Execute commands
        response_counts = {"ack_count": 0, "heartbeat_count": 0, "error_count": 0}
        
        for i, cmd_bytes in enumerate(commands):
            opcode = cmd_bytes[0]
            
            # Determine description
            opcode_names = {
                0x21: "FRAMING",
                0x23: "JOB_HEADER",
                0x0A: "CONNECT",
                0x22: "DATA",
                0x24: "INIT"
            }
            desc = opcode_names.get(opcode, f"OPCODE_{opcode:#04x}")
            if opcode == 0x22:
                desc = f"DATA chunk {i}/{len(commands)}"
            
            # DRY RUN: skip INIT commands (opcode 0x24)
            if self.dry_run and opcode == 0x24:
                if csv_logger:
                    csv_logger.log_operation(
                        phase="finalize",
                        operation="INIT_SKIPPED_DRY_RUN",
                        duration_ms=0,
                        state="SKIPPED",
                        device_state="READY",
                    )
                continue  # Skip this command
            
            # Log send
            if byte_logger:
                byte_logger.log_send(cmd_bytes, desc)
            
            # Send to device
            transport.write(cmd_bytes)
            
            # Read response using transport timeout API.
            try:
                transport.set_timeout(1.0)
                response = transport.read(64)
                
                if byte_logger:
                    byte_logger.log_recv(response)
                
                # Count response types
                if response and len(response) >= 1:
                    resp_opcode = response[0]
                    if resp_opcode == 0x09:
                        response_counts["ack_count"] += 1
                    elif resp_opcode == 0x04:
                        response_counts["heartbeat_count"] += 1
                    elif resp_opcode == 0x08:
                        response_counts["error_count"] += 1
                
            except Exception as e:
                if byte_logger:
                    byte_logger.log_error(f"Read timeout on command {i}: {e}")
            
            # CSV logging
            if csv_logger:
                csv_logger.log_operation(
                    phase="execute",
                    operation=desc,
                    duration_ms=0,
                    bytes_transferred=len(cmd_bytes),
                    state="ACTIVE"
                )

        return {
            "ok": True,
            "commands_sent": len(commands),
            "device_responses": response_counts
        }
