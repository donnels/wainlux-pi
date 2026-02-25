"""K6 laser device service for managing device operations"""

from typing import Optional
import logging
import threading
import time

import os

from k6.driver import WainluxK6
from k6.transport import SerialTransport, MockTransport
from k6 import protocol
from k6.byte_logger import ByteDumpLogger

logger = logging.getLogger(__name__)


class K6DeviceManager:
    """Manages K6 device connection and state"""

    def __init__(self):
        self.driver = WainluxK6()
        self.connected = False
        self.version: Optional[str] = None
        self._dry_run = False  # Driver-level dry run toggle
        self.serial_lock = threading.RLock()
        self.mock_mode = os.getenv("K6_MOCK_DEVICE", "false").lower() == "true"
        
        # Initialize transport based on mock mode
        if self.mock_mode:
            self.transport = MockTransport(auto_respond=True, version=(0, 0, 1))
            logger.info("K6DeviceManager initialized with MockTransport")
        else:
            self.transport: Optional[SerialTransport] = None
            logger.info("K6DeviceManager initialized (transport will be created on connect)")
    
    @property
    def dry_run(self) -> bool:
        """Get dry run state"""
        return self._dry_run
    
    @dry_run.setter
    def dry_run(self, value: bool):
        """Set dry run state and sync to driver"""
        self._dry_run = bool(value)
        self.driver.dry_run = self._dry_run

    def connect(self, port: str = "/dev/ttyUSB0", baudrate: int = 115200, timeout: float = 2.0) -> tuple[bool, Optional[str]]:
        """Connect to K6 device.

        Args:
            port: Serial port device path
            baudrate: Serial baud rate
            timeout: Connection timeout in seconds

        Returns:
            Tuple of (success, version_string)
        """
        try:
            if self.mock_mode:
                self.transport = MockTransport(auto_respond=True, version=(0, 0, 1))
            else:
                self.transport = SerialTransport(port=port, baudrate=baudrate, timeout=timeout)

            success, version = self.driver.connect_transport(self.transport)
            if success:
                self.connected = True
                self.version = f"v{version[0]}.{version[1]}.{version[2]}"
                logger.info(f"Connected to K6 {self.version}{' (mock)' if self.mock_mode else ''}")
                return True, self.version
            else:
                self.connected = False
                self.transport = None
                return False, None
        except Exception as e:
            logger.error(f"Connect failed: {e}")
            self.connected = False
            self.transport = None
            return False, None

    def disconnect(self) -> bool:
        """Disconnect from K6 device.

        Returns:
            True if disconnected successfully
        """
        if self.transport:
            try:
                self.transport.close()
            except Exception as e:
                logger.warning(f"Disconnect warning: {e}")

        self.transport = None
        self.connected = False
        self.version = None
        logger.info("Disconnected from K6")
        return True

    def is_connected(self) -> bool:
        """Check if device is connected"""
        return self.connected and self.transport is not None


class K6Service:
    """Service for K6 laser operations"""

    def __init__(self, device_manager: K6DeviceManager):
        self.device = device_manager

    @staticmethod
    def parse_hex_bytes(packet_hex: str) -> bytes:
        """Parse hex text into bytes.

        Supports formats like:
        - "ff000400"
        - "ff 00 04 00"
        - "0xFF,0x00,0x04,0x00"
        """
        if not packet_hex or not isinstance(packet_hex, str):
            raise ValueError("packet_hex is required")

        normalized = (
            packet_hex.replace("0x", "")
            .replace("0X", "")
            .replace(",", " ")
            .replace("\n", " ")
            .strip()
        )
        hex_text = "".join(normalized.split())
        if len(hex_text) % 2 != 0:
            raise ValueError("Hex payload must contain an even number of characters")

        try:
            return bytes.fromhex(hex_text)
        except ValueError as exc:
            raise ValueError(f"Invalid hex payload: {exc}") from exc

    def raw_send(
        self,
        payload: bytes,
        timeout: float = 2.0,
        read_size: int = 256,
        flush_input: bool = False,
        settle_ms: int = 120,
        log_prefix: Optional[str] = None,
    ) -> tuple[bool, dict]:
        """Send raw bytes to device and collect raw response bytes.

        This bypasses protocol validation intentionally for opcode testing.
        """
        if not self.device.transport:
            return False, {"success": False, "error": "Device not connected"}
        if not payload:
            return False, {"success": False, "error": "Empty payload"}

        timeout = max(0.05, min(float(timeout), 30.0))
        read_size = max(1, min(int(read_size), 4096))
        settle_ms = max(10, min(int(settle_ms), 1000))

        byte_logger = None
        response = bytearray()
        tx_time = time.time()

        try:
            if log_prefix:
                timestamp = time.strftime("%Y%m%d_%H%M%S")
                base_path = f"{log_prefix}_{timestamp}"
                byte_logger = ByteDumpLogger(base_path)

            with self.device.serial_lock:
                transport = self.device.transport
                if flush_input:
                    transport.reset_input_buffer()
                transport.set_timeout(timeout)

                if byte_logger:
                    byte_logger.log_send(payload, "RAW_SEND")
                bytes_written = transport.write(payload)

                deadline = time.time() + timeout
                idle_deadline = time.time() + (settle_ms / 1000.0)
                while time.time() < deadline and len(response) < read_size:
                    chunk = transport.read(1)
                    if chunk:
                        response.extend(chunk)
                        idle_deadline = time.time() + (settle_ms / 1000.0)
                    elif response and time.time() >= idle_deadline:
                        break

                if byte_logger and response:
                    byte_logger.log_recv(bytes(response))

            hb_count, ack_count, response_type = protocol.parse_response_frames(bytes(response))
            return True, {
                "success": True,
                "tx_hex": payload.hex(),
                "tx_len": len(payload),
                "bytes_written": bytes_written,
                "rx_hex": bytes(response).hex(),
                "rx_len": len(response),
                "response_type": response_type,
                "ack_count": ack_count,
                "heartbeat_count": hb_count,
                "timeout": timeout,
                "read_size": read_size,
                "flush_input": flush_input,
                "elapsed_ms": round((time.time() - tx_time) * 1000, 2),
            }
        except Exception as e:
            logger.error(f"Raw send failed: {e}")
            if byte_logger:
                byte_logger.log_error(str(e))
            return False, {"success": False, "error": str(e)}
        finally:
            if byte_logger:
                byte_logger.close()

    def draw_bounds(self, width: int = 1600, height: int = 1520, 
                    center_x: int = 800, center_y: int = 760) -> bool:
        """Draw preview bounds rectangle.

        Args:
            width: Width in pixels
            height: Height in pixels
            center_x: Center X position
            center_y: Center Y position

        Returns:
            True if successful
        """
        if not self.device.is_connected():
            return False

        try:
            return self.device.driver.draw_bounds_transport(
                self.device.transport,
                width=width,
                height=height,
                center_x=center_x,
                center_y=center_y
            )
        except Exception as e:
            logger.error(f"Draw bounds failed: {e}")
            return False

    def home(self) -> bool:
        """Home the laser to origin (0,0).

        Returns:
            True if successful
        """
        try:
            # Stop preview mode first
            protocol.send_cmd_checked(
                self.device.transport,
                "FRAMING (stop preview)",
                bytes([0x21, 0x00, 0x04, 0x00]),
                timeout=2.0,
                expect_ack=True,
            )

            # Then send HOME
            protocol.send_cmd_checked(
                self.device.transport,
                "HOME",
                bytes([0x17, 0x00, 0x04, 0x00]),
                timeout=10.0,
                expect_ack=True,
            )
            return True
        except Exception as e:
            logger.error(f"Home failed: {e}")
            return False

    def jog(self, center_x: int, center_y: int, disable_limits: bool = False) -> tuple[bool, dict]:
        """Move laser head to position.

        Args:
            center_x: X position in pixels
            center_y: Y position in pixels
            disable_limits: Allow movement beyond soft limits

        Returns:
            Tuple of (success, result_dict)
        """
        try:
            # Use BOUNDS with 1x1px to position laser
            success = self.device.driver.draw_bounds_transport(
                self.device.transport,
                width=1,
                height=1,
                center_x=center_x,
                center_y=center_y
            )
            if success:
                return True, {
                    "success": True,
                    "message": f"Moved to ({center_x}, {center_y})",
                    "x": center_x,
                    "y": center_y
                }
            else:
                return False, {"success": False, "error": "BOUNDS command failed"}
        except Exception as e:
            logger.error(f"Jog failed: {e}")
            return False, {"success": False, "error": str(e)}

    def mark(self, center_x: int, center_y: int, power: int = 500, depth: int = 10) -> tuple[bool, dict]:
        """Burn a single pixel mark at position.

        Args:
            center_x: X position in pixels
            center_y: Y position in pixels
            power: Laser power (0-1000)
            depth: Burn depth (1-255)

        Returns:
            Tuple of (success, result_dict)
        """
        try:
            success = self.device.driver.mark_position_transport(
                self.device.transport,
                center_x=center_x,
                center_y=center_y,
                power=power,
                depth=depth
            )
            if success:
                return True, {
                    "success": True,
                    "message": f"Marked at ({center_x}, {center_y})",
                    "x": center_x,
                    "y": center_y,
                    "power": power,
                    "depth": depth
                }
            else:
                return False, {"success": False, "error": "Mark command failed"}
        except Exception as e:
            logger.error(f"Mark failed: {e}")
            return False, {"success": False, "error": str(e)}

    def crosshair(self, enable: bool) -> tuple[bool, dict]:
        """Toggle crosshair/positioning laser.

        Args:
            enable: True to turn on, False to turn off

        Returns:
            Tuple of (success, result_dict)
        """
        try:
            opcode = 0x06 if enable else 0x07
            cmd_name = "CROSSHAIR ON" if enable else "CROSSHAIR OFF"

            protocol.send_cmd_checked(
                self.device.transport,
                cmd_name,
                bytes([opcode, 0x00, 0x04, 0x00]),
                timeout=2.0,
                expect_ack=True,
            )
            return True, {
                "success": True,
                "message": cmd_name,
                "enabled": enable
            }
        except Exception as e:
            logger.error(f"Crosshair failed: {e}")
            return False, {"success": False, "error": str(e)}

    def stop(self) -> tuple[bool, str]:
        """Emergency stop and reset device.

        Returns:
            Tuple of (success, message)
        """
        if not self.device.transport:
            return False, "No transport available"

        try:
            # Send STOP command
            self.device.transport.write(bytes([0x16, 0x00, 0x04, 0x00]))
            
            # Send CONNECT to reset device state
            protocol.send_cmd_checked(
                self.device.transport,
                "CONNECT #1",
                bytes([0x0A, 0x00, 0x04, 0x00]),
                timeout=2.0,
                expect_ack=True,
            )
            logger.info("STOP + CONNECT sent, device reset")
            return True, "Device stopped and reset"
        except Exception as e:
            logger.warning(f"Stop/reset partial: {e}")
            return True, f"Stop signal sent (reset failed: {e})"

    def engrave(self, image_path: str, power: int = 1000, depth: int = 100, 
                center_x: Optional[int] = None, center_y: Optional[int] = None, 
                csv_logger=None, dry_run_override: Optional[bool] = None) -> dict:
        """Engrave an image.

        Args:
            image_path: Path to image file
            power: Laser power (0-1000)
            depth: Burn depth (1-255)
            center_x: Center X position (optional)
            center_y: Center Y position (optional)
            csv_logger: Optional CSV logger for burn tracking
            dry_run_override: Optional per-request dry-run setting (does not persist)

        Returns:
            Dict with keys: ok (bool), message (str), total_time (float), chunks (int)
        """
        try:
            if not self.device.transport:
                return {"ok": False, "message": "Device not connected", "total_time": 0, "chunks": 0}

            logger.info("Engrave request waiting for serial lock")
            # Serialize all burn operations so protocol frames cannot interleave.
            with self.device.serial_lock:
                logger.info("Engrave request acquired serial lock")
                previous_dry_run = self.device.dry_run
                if dry_run_override is not None:
                    self.device.dry_run = bool(dry_run_override)
                try:
                    result = self.device.driver.engrave_transport(
                        self.device.transport,
                        image_path,
                        power=power,
                        depth=depth,
                        center_x=center_x,
                        center_y=center_y,
                        csv_logger=csv_logger
                    )
                finally:
                    if dry_run_override is not None:
                        self.device.dry_run = previous_dry_run

            logger.info("Engrave request released serial lock")
            return result
        except Exception as e:
            logger.error(f"Engrave failed: {e}")
            return {"ok": False, "message": str(e), "total_time": 0, "chunks": 0}
