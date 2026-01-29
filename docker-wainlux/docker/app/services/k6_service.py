"""K6 laser device service for managing device operations"""

from typing import Optional
import logging

from k6.driver import WainluxK6
from k6.transport import SerialTransport
from k6 import protocol

logger = logging.getLogger(__name__)


class K6DeviceManager:
    """Manages K6 device connection and state"""

    def __init__(self):
        self.driver = WainluxK6()
        self.transport: Optional[SerialTransport] = None
        self.connected = False
        self.version: Optional[str] = None

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
            self.transport = SerialTransport(port=port, baudrate=baudrate, timeout=timeout)
            success, version = self.driver.connect_transport(self.transport)
            if success:
                self.connected = True
                self.version = f"v{version[0]}.{version[1]}.{version[2]}"
                logger.info(f"Connected to K6 {self.version}")
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
        if not self.device.is_connected():
            return False

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

    def jog(self, center_x: int, center_y: int) -> bool:
        """Move laser head to position.

        Args:
            center_x: X position in pixels
            center_y: Y position in pixels

        Returns:
            True if successful
        """
        if not self.device.is_connected():
            return False

        try:
            # Use BOUNDS with 1x1px to position laser
            return self.device.driver.draw_bounds_transport(
                self.device.transport,
                width=1,
                height=1,
                center_x=center_x,
                center_y=center_y
            )
        except Exception as e:
            logger.error(f"Jog failed: {e}")
            return False

    def mark(self, center_x: int, center_y: int, power: int = 500, depth: int = 10) -> bool:
        """Burn a single pixel mark at position.

        Args:
            center_x: X position in pixels
            center_y: Y position in pixels
            power: Laser power (0-1000)
            depth: Burn depth (1-255)

        Returns:
            True if successful
        """
        if not self.device.is_connected():
            return False

        try:
            return self.device.driver.mark_position_transport(
                self.device.transport,
                center_x=center_x,
                center_y=center_y,
                power=power,
                depth=depth
            )
        except Exception as e:
            logger.error(f"Mark failed: {e}")
            return False

    def crosshair(self, enable: bool) -> bool:
        """Toggle crosshair/positioning laser.

        Args:
            enable: True to turn on, False to turn off

        Returns:
            True if successful
        """
        if not self.device.is_connected():
            return False

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
            return True
        except Exception as e:
            logger.error(f"Crosshair toggle failed: {e}")
            return False

    def stop(self) -> bool:
        """Emergency stop and reset device.

        Returns:
            True if successful
        """
        if not self.device.transport:
            return False

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
            return True
        except Exception as e:
            logger.warning(f"Stop/reset partial: {e}")
            return True  # Return True anyway since cancel signal sent

    def engrave(self, image_path: str, power: int = 1000, depth: int = 100, 
                center_x: Optional[int] = None, center_y: Optional[int] = None, 
                csv_logger=None) -> dict:
        """Engrave an image.

        Args:
            image_path: Path to image file
            power: Laser power (0-1000)
            depth: Burn depth (1-255)
            center_x: Center X position (optional)
            center_y: Center Y position (optional)
            csv_logger: Optional CSV logger for burn tracking

        Returns:
            Dict with keys: ok (bool), message (str), total_time (float), chunks (int)
        """
        if not self.device.is_connected():
            return {"ok": False, "message": "Not connected", "total_time": 0, "chunks": 0}

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
            return result
        except Exception as e:
            logger.error(f"Engrave failed: {e}")
            return {"ok": False, "message": str(e), "total_time": 0, "chunks": 0}
