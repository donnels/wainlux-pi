"""K6 Command Abstraction Layer

Separates command generation from execution to enable:
- Preview/validation without hardware
- Unit testing of command generation
- Protocol debugging and analysis
"""

from __future__ import annotations
from dataclasses import dataclass, field
from typing import List, Optional, Dict, Any
from enum import IntEnum
import time


class K6Opcode(IntEnum):
    """K6 protocol command opcodes"""
    VERSION = 0xFF
    BOUNDS = 0x20
    FRAMING = 0x21
    DATA = 0x22
    JOB_HEADER = 0x23
    INIT = 0x24
    SET_SPEED_POWER = 0x25
    SET_FOCUS_ANGLE = 0x28
    CROSSHAIR_ON = 0x06
    CROSSHAIR_OFF = 0x07
    CONNECT = 0x0A
    HOME = 0x17
    STOP = 0x16
    RESET_MCU = 0xFE


@dataclass
class K6Command:
    """A single K6 protocol command.
    
    Represents what will be sent to hardware, but as a data structure
    that can be inspected, tested, and previewed before execution.
    """
    opcode: K6Opcode
    payload: bytes
    description: str
    expect_ack: bool = True
    timeout: float = 2.0
    phase: str = "operation"  # For CSV logging grouping
    
    # Metadata for preview/analysis
    bytes_transferred: int = field(init=False)
    timestamp: float = field(default_factory=time.time)
    
    def __post_init__(self):
        self.bytes_transferred = len(self.payload)
    
    def to_bytes(self) -> bytes:
        """Serialize command to bytes for transmission or storage.
        
        Returns the payload directly since it already includes the
        4-byte header (opcode, reserved, length_le16) followed by data.
        """
        return self.payload
    
    def to_dict(self) -> Dict[str, Any]:
        """Serialize for JSON/preview"""
        return {
            'opcode': f"0x{self.opcode:02X}",
            'opcode_name': self.opcode.name,
            'description': self.description,
            'size_bytes': self.bytes_transferred,
            'expect_ack': self.expect_ack,
            'timeout': self.timeout,
            'phase': self.phase,
            'payload_hex': self.payload[:32].hex() + ('...' if len(self.payload) > 32 else ''),
        }
    
    def __repr__(self) -> str:
        return f"K6Command({self.opcode.name}, {self.description}, {self.bytes_transferred}B)"


class K6CommandBuilder:
    """Builds K6 protocol commands without executing them.
    
    This class contains all the command-building logic extracted from
    the driver and protocol modules. Each method returns a K6Command
    that can be previewed, validated, or executed.
    """
    
    # Constants from protocol
    ACK = 0x09
    DATA_CHUNK = 1900
    
    @staticmethod
    def checksum(packet: bytes) -> int:
        """Two's complement (8-bit) checksum"""
        total = sum(packet[:-1]) if len(packet) >= 1 else 0
        return (-total) & 0xFF
    
    @classmethod
    def build_framing(cls) -> K6Command:
        """Build FRAMING (0x21) command - stops preview mode.
        
        BOUNDS (0x20) enables preview, FRAMING (0x21) disables it and
        officially starts the burn sequence.
        """
        return K6Command(
            opcode=K6Opcode.FRAMING,
            payload=bytes([0x21, 0x00, 0x04, 0x00]),
            description="FRAMING - stop preview mode",
            expect_ack=True,
            timeout=1.0,
            phase="setup"
        )
    
    @classmethod
    def build_connect(cls, number: int = 1) -> K6Command:
        """Build CONNECT (0x0A) command.
        
        Typically sent 2x before burn and 2x after INIT.
        """
        return K6Command(
            opcode=K6Opcode.CONNECT,
            payload=bytes([0x0A, 0x00, 0x04, 0x00]),
            description=f"CONNECT #{number}",
            expect_ack=True,
            timeout=1.0,
            phase="setup"
        )
    
    @classmethod
    def build_bounds(
        cls,
        width: int,
        height: int,
        center_x: Optional[int] = None,
        center_y: Optional[int] = None
    ) -> K6Command:
        """Build BOUNDS (0x20) command for preview rectangle.
        
        Fast preview - single 11-byte command showing burn area.
        """
        if center_x is None:
            center_x = (width // 2) + 67  # Match JOB_HEADER positioning
        if center_y is None:
            center_y = height // 2
        
        # Packet structure (11 bytes):
        # [0x20][0x00][0x0B][W_msb][W_lsb][H_msb][H_lsb][X_msb][X_lsb][Y_msb][Y_lsb]
        pkt = bytearray([0x20, 0x00, 0x0B])
        pkt.extend([(width >> 8) & 0xFF, width & 0xFF])
        pkt.extend([(height >> 8) & 0xFF, height & 0xFF])
        pkt.extend([(center_x >> 8) & 0xFF, center_x & 0xFF])
        pkt.extend([(center_y >> 8) & 0xFF, center_y & 0xFF])
        
        return K6Command(
            opcode=K6Opcode.BOUNDS,
            payload=bytes(pkt),
            description=f"BOUNDS {width}×{height} @ ({center_x}, {center_y})",
            expect_ack=True,
            timeout=2.0,
            phase="preview"
        )
    
    @classmethod
    def build_job_header(
        cls,
        width: int,
        height: int,
        depth: int,
        power: int = 1000,
        center_x: Optional[int] = None,
        center_y: Optional[int] = None,
    ) -> K6Command:
        """Build JOB_HEADER (0x23) for raster-only burn.
        
        Configures burn parameters and positioning.
        """
        # Calculate total payload size (packed pixels)
        bytes_per_line = (width + 7) // 8
        total_size = bytes_per_line * height
        
        # Center point calculation
        if center_x is None:
            center_x = (width // 2) + 67
        if center_y is None:
            center_y = 760  # Center of 1520px work area
        
        # Build 38-byte header
        hdr = bytearray(38)
        hdr[0] = 0x23
        hdr[1] = 0x00
        hdr[2] = 38
        
        def put_u16_be(idx: int, val: int):
            hdr[idx] = (val >> 8) & 0xFF
            hdr[idx + 1] = val & 0xFF
        
        def put_u32_be(idx: int, val: int):
            hdr[idx] = (val >> 24) & 0xFF
            hdr[idx + 1] = (val >> 16) & 0xFF
            hdr[idx + 2] = (val >> 8) & 0xFF
            hdr[idx + 3] = val & 0xFF
        
        # Packet count heuristic
        param1 = (total_size // 4094) + 1
        put_u16_be(3, param1)
        hdr[5] = 0x01
        
        put_u16_be(6, width)
        put_u16_be(8, height)
        put_u16_be(10, 33)  # Unknown constant
        put_u16_be(12, power)
        put_u16_be(14, depth)
        put_u16_be(16, 0)  # vector_w
        put_u16_be(18, 0)  # vector_h
        put_u32_be(20, total_size)
        put_u16_be(24, 0)  # vector_power
        put_u16_be(26, 0)  # vector_depth
        put_u32_be(28, 0)  # point_count
        put_u16_be(32, center_x)
        put_u16_be(34, center_y)
        hdr[36] = 1  # quality
        hdr[37] = 0x00
        
        return K6Command(
            opcode=K6Opcode.JOB_HEADER,
            payload=bytes(hdr),
            description=f"JOB_HEADER {width}×{height} power={power} depth={depth} @ ({center_x}, {center_y})",
            expect_ack=False,  # Device responds with HEARTBEAT, not ACK
            timeout=10.0,
            phase="setup"
        )
    
    @classmethod
    def build_data_packet(cls, payload: bytes, line_num: int = 0) -> K6Command:
        """Build DATA (0x22) packet for raster line data.
        
        Layout:
          0: opcode 0x22
          1-2: length (big-endian)
          3..N-2: payload
          N-1: checksum
        """
        if len(payload) > cls.DATA_CHUNK:
            raise ValueError(f"Payload too large: {len(payload)} > {cls.DATA_CHUNK}")
        
        packet_len = len(payload) + 4
        pkt = bytearray(packet_len)
        pkt[0] = 0x22
        pkt[1] = (packet_len >> 8) & 0xFF
        pkt[2] = packet_len & 0xFF
        pkt[3 : 3 + len(payload)] = payload
        pkt[-1] = cls.checksum(pkt)
        
        return K6Command(
            opcode=K6Opcode.DATA,
            payload=bytes(pkt),
            description=f"DATA line {line_num} ({len(payload)} bytes)",
            expect_ack=True,
            timeout=2.0,
            phase="burn"
        )
    
    @classmethod
    def build_init(cls, number: int = 1) -> K6Command:
        """Build INIT (0x24) command.
        
        Sent 2x after burn. Device responds with status frames (FF FF 00 XX).
        """
        payload = bytes([0x24, 0x00, 0x0B, 0x00] + [0x00] * 7)
        return K6Command(
            opcode=K6Opcode.INIT,
            payload=payload,
            description=f"INIT #{number}",
            expect_ack=False,  # Returns status frames
            timeout=3.0,
            phase="finalize"
        )
    
    @classmethod
    def build_home(cls) -> K6Command:
        """Build HOME (0x17) command."""
        return K6Command(
            opcode=K6Opcode.HOME,
            payload=bytes([0x17, 0x00, 0x04, 0x00]),
            description="HOME",
            expect_ack=True,
            timeout=5.0,
            phase="operation"
        )
    
    @classmethod
    def build_stop(cls) -> K6Command:
        """Build STOP (0x16) command - emergency stop."""
        return K6Command(
            opcode=K6Opcode.STOP,
            payload=bytes([0x16, 0x00, 0x04, 0x00]),
            description="STOP",
            expect_ack=True,
            timeout=1.0,
            phase="operation"
        )
    
    @classmethod
    def build_crosshair(cls, enable: bool) -> K6Command:
        """Build crosshair laser command (0x06 ON, 0x07 OFF)."""
        opcode = K6Opcode.CROSSHAIR_ON if enable else K6Opcode.CROSSHAIR_OFF
        state = "ON" if enable else "OFF"
        return K6Command(
            opcode=opcode,
            payload=bytes([opcode, 0x00, 0x04, 0x00]),
            description=f"CROSSHAIR {state}",
            expect_ack=True,
            timeout=1.0,
            phase="operation"
        )
    
    @classmethod
    def build_version(cls) -> K6Command:
        """Build VERSION (0xFF) query command."""
        return K6Command(
            opcode=K6Opcode.VERSION,
            payload=bytes([0xFF, 0x00, 0x04, 0x00]),
            description="VERSION",
            expect_ack=False,  # Returns 3-byte version
            timeout=1.0,
            phase="connect"
        )


class CommandSequence:
    """A sequence of K6 commands representing a complete job.
    
    Used for preview, validation, and eventual execution.
    """
    
    def __init__(self, description: str = ""):
        self.commands: List[K6Command] = []
        self.description = description
        self.stats = {
            'total_commands': 0,
            'total_bytes': 0,
            'data_chunks': 0,
            'estimated_time_sec': 0.0,
        }
    
    def add(self, command: K6Command) -> None:
        """Add command to sequence and update stats"""
        self.commands.append(command)
        self.stats['total_commands'] += 1
        self.stats['total_bytes'] += command.bytes_transferred
        if command.opcode == K6Opcode.DATA:
            self.stats['data_chunks'] += 1
        # Rough time estimate: timeout per command + data transfer time
        self.stats['estimated_time_sec'] += command.timeout
        if command.opcode == K6Opcode.DATA:
            # Data chunks take ~0.1s each observed
            self.stats['estimated_time_sec'] += 0.1
    
    def get_bounds(self) -> Dict[str, int]:
        """Extract bounds from JOB_HEADER commands"""
        for cmd in self.commands:
            if cmd.opcode == K6Opcode.JOB_HEADER:
                # Parse center_x, center_y from payload (bytes 32-35)
                if len(cmd.payload) >= 36:
                    center_x = (cmd.payload[32] << 8) | cmd.payload[33]
                    center_y = (cmd.payload[34] << 8) | cmd.payload[35]
                    width = (cmd.payload[6] << 8) | cmd.payload[7]
                    height = (cmd.payload[8] << 8) | cmd.payload[9]
                    return {
                        'center_x': center_x,
                        'center_y': center_y,
                        'width': width,
                        'height': height,
                        'min_x': center_x - width // 2,
                        'max_x': center_x + width // 2,
                        'min_y': center_y - height // 2,
                        'max_y': center_y + height // 2,
                    }
        return {}
    
    def to_dict(self) -> Dict[str, Any]:
        """Serialize for JSON/API"""
        return {
            'description': self.description,
            'stats': self.stats,
            'bounds': self.get_bounds(),
            'commands': [cmd.to_dict() for cmd in self.commands],
        }
    
    def summary(self) -> str:
        """Human-readable summary"""
        bounds = self.get_bounds()
        lines = [
            f"Job: {self.description}",
            f"Commands: {self.stats['total_commands']} ({self.stats['data_chunks']} data chunks)",
            f"Size: {self.stats['total_bytes']:,} bytes",
            f"Est. Time: {self.stats['estimated_time_sec']:.1f}s",
        ]
        if bounds:
            lines.append(f"Bounds: {bounds['width']}×{bounds['height']} @ ({bounds['center_x']}, {bounds['center_y']})")
            lines.append(f"  X: {bounds['min_x']} → {bounds['max_x']}")
            lines.append(f"  Y: {bounds['min_y']} → {bounds['max_y']}")
        return '\n'.join(lines)
