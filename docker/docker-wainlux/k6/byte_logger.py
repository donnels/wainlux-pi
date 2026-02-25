"""Raw byte logger for serial I/O inspection

Captures ALL serial communication without filtering or validation.
Like `tee` in Unix - logs everything passing through.

Used for:
- Protocol debugging
- Device response analysis
- Replay verification
- Troubleshooting failed burns
"""

from datetime import datetime, timezone
from pathlib import Path


class ByteDumpLogger:
    """Log raw serial I/O for protocol analysis.
    
    Creates two files:
    - .dump: Binary dump of all I/O
    - .dump.txt: Human-readable hex/decimal format
    
    NO validation, NO filtering - pure data capture.
    """

    @staticmethod
    def _iso_timestamp() -> str:
        """UTC ISO-8601 timestamp with millisecond precision."""
        return datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")

    def __init__(self, base_path: str):
        """Initialize byte logger.
        
        Args:
            base_path: Base path for log files (without extension)
                      Creates: {base_path}.dump and {base_path}.dump.txt
        """
        self.base_path = Path(base_path)
        self.binary_file = open(f"{base_path}.dump", 'wb')
        self.text_file = open(f"{base_path}.dump.txt", 'w')
        
        # Write header
        self.text_file.write(f"K6 Serial I/O Dump - {self._iso_timestamp()}\n")
        self.text_file.write("=" * 70 + "\n\n")
        self.text_file.flush()

    def log_send(self, data: bytes, description: str = ""):
        """Log outgoing bytes to device.
        
        Args:
            data: Bytes sent to device
            description: Optional description (e.g. "FRAMING", "DATA chunk 5/100")
        """
        timestamp = self._iso_timestamp()
        
        # Binary dump
        self.binary_file.write(b">>> SEND " + data + b"\n")
        self.binary_file.flush()
        
        # Text dump
        self.text_file.write(f"[{timestamp}] SEND ({len(data)} bytes)")
        if description:
            self.text_file.write(f": {description}")
        self.text_file.write("\n")
        
        # Hex format (16 bytes per line)
        self.text_file.write("  HEX: ")
        for i, byte in enumerate(data):
            self.text_file.write(f"{byte:02x} ")
            if (i + 1) % 16 == 0 and i < len(data) - 1:
                self.text_file.write("\n       ")
        self.text_file.write("\n")
        
        # Decimal format
        self.text_file.write("  DEC: ")
        for i, byte in enumerate(data):
            self.text_file.write(f"{byte:3d} ")
            if (i + 1) % 16 == 0 and i < len(data) - 1:
                self.text_file.write("\n       ")
        self.text_file.write("\n\n")
        self.text_file.flush()

    def log_recv(self, data: bytes):
        """Log incoming bytes from device.
        
        Args:
            data: Bytes received from device
        """
        if not data:
            return
            
        timestamp = self._iso_timestamp()
        
        # Binary dump
        self.binary_file.write(b"<<< RECV " + data + b"\n")
        self.binary_file.flush()
        
        # Text dump
        self.text_file.write(f"[{timestamp}] RECV ({len(data)} bytes)\n")
        
        # Hex format
        self.text_file.write("  HEX: ")
        for i, byte in enumerate(data):
            self.text_file.write(f"{byte:02x} ")
            if (i + 1) % 16 == 0 and i < len(data) - 1:
                self.text_file.write("\n       ")
        self.text_file.write("\n")
        
        # Decimal format
        self.text_file.write("  DEC: ")
        for i, byte in enumerate(data):
            self.text_file.write(f"{byte:3d} ")
            if (i + 1) % 16 == 0 and i < len(data) - 1:
                self.text_file.write("\n       ")
        self.text_file.write("\n")
        
        # Interpret common responses
        if len(data) >= 1:
            opcode = data[0]
            interpretations = {
                0x09: "ACK",
                0x08: "ERROR",
                0x04: "HEARTBEAT",
                0x05: "STATUS"
            }
            if opcode in interpretations:
                self.text_file.write(f"  → {interpretations[opcode]}\n")
        
        self.text_file.write("\n")
        self.text_file.flush()

    def log_error(self, message: str):
        """Log error message.
        
        Args:
            message: Error description
        """
        timestamp = self._iso_timestamp()
        self.text_file.write(f"[{timestamp}] ERROR: {message}\n\n")
        self.text_file.flush()

    def close(self):
        """Close log files."""
        if self.binary_file:
            self.binary_file.close()
        if self.text_file:
            self.text_file.write(f"\nLog closed: {self._iso_timestamp()}\n")
            self.text_file.close()

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        self.close()
