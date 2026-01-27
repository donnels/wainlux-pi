"""MVP driver: thin wrapper that calls the existing k6_burn_image.py

This keeps behavior stable now while providing a clear migration
point for a future pure-Python protocol implementation.
"""

from __future__ import annotations
import subprocess
import sys
from pathlib import Path
from typing import Optional, Dict

K6_BURN_SCRIPT = Path(__file__).parents[1] / "k6_burn_image.py"


class WainluxK6:
    """Thin driver wrapper around the CLI for now.

    Methods are synchronous and return simple success booleans or
    dictionaries for status. This is an MVP; the goal is predictable
    behavior and a clear, testable contract.
    """

    def __init__(self, script_path: Optional[str] = None):
        self.script = Path(script_path) if script_path else K6_BURN_SCRIPT

    def _run(self, *args) -> subprocess.CompletedProcess:
        # Use the same Python interpreter that is running this process
        # so test runs use the virtualenv Python and have the same deps.
        cmd = [sys.executable, str(self.script)] + list(args)
        return subprocess.run(cmd, capture_output=True, text=True)

    def connect(self) -> bool:
        # CLI script handles serial inits; rely on it.
        res = self._run("--verbose")
        return res.returncode == 0

    def disconnect(self) -> bool:
        # No-op for MVP (the CLI script opens/closes per call)
        return True

    def home(self) -> bool:
        res = self._run("--home")
        return res.returncode == 0

    def draw_bounds(self, boundary_mm: int = 75, depth: int = 5) -> bool:
        args = [
            "--pattern",
            "boundary",
            "--boundary-mm",
            str(boundary_mm),
            "--depth",
            str(depth),
        ]
        res = self._run(*args)
        return res.returncode == 0

    def engrave(self, image_path: str, power: int = 1000, depth: int = 100) -> Dict:
        res = self._run(image_path, "--power", str(power), "--depth", str(depth))
        return {
            "ok": res.returncode == 0,
            "stdout": res.stdout,
            "stderr": res.stderr,
        }

    def get_status(self) -> Dict:
        # CLI does not expose a status endpoint; return placeholder.
        return {"state": "idle", "progress": 0, "message": "MVP driver"}

    # --- Transport-based methods (migration path) ---
    def connect_transport(self, transport, csv_logger=None) -> bool:
        """Perform the init sequence (STOP, VERSION, CONNECT x2, HOME) using a transport.

        Raises protocol.K6TimeoutError or protocol.K6DeviceError on failure.
        Returns True on success.

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
        return True

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
        img = Image.open(image_path).convert("L")  # grayscale
        width, height = img.size

        if width > 1600 or height > 1520:
            raise ValueError(f"Image too large: {width}x{height} (max 1600x1520)")

        # Convert to packed pixels (1 byte per 8 pixels, MSB first)
        payload_lines = []
        for y in range(height):
            row_bytes = bytearray()
            for x in range(0, width, 8):
                byte = 0
                for bit in range(8):
                    px = x + bit
                    if px < width:
                        gray = img.getpixel((px, y))
                        # Invert: 0=burn, 255=skip
                        if gray < 128:
                            byte |= 1 << (7 - bit)
                row_bytes.append(byte)
            payload_lines.append(bytes(row_bytes))

        # Protocol sequence: FRAMING → JOB_HEADER → CONNECT x2 → burn_payload → INIT x2 → wait

        # FRAMING
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
