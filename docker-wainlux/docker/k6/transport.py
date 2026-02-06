"""Transport abstractions for K6 protocol (Serial + Mock)

Keep this small and explicit. Real SerialTransport wraps pyserial. MockTransport
is for unit tests and simulates byte-at-a-time reads and queued responses.
"""

from __future__ import annotations


class TransportBase:
    def write(self, data: bytes) -> int:  # returns bytes written
        raise NotImplementedError

    def read(self, size: int = 1) -> bytes:
        raise NotImplementedError

    def set_timeout(self, timeout: float):
        raise NotImplementedError

    def reset_input_buffer(self):
        raise NotImplementedError

    def reset_output_buffer(self):
        raise NotImplementedError

    def close(self):
        raise NotImplementedError


class SerialTransport(TransportBase):
    def __init__(
        self, port: str = "/dev/ttyUSB0", baudrate: int = 115200, timeout: float = 2.0
    ):
        import serial

        self._ser = serial.Serial(port, baudrate, timeout=timeout)

    def write(self, data: bytes) -> int:
        return self._ser.write(data)

    def read(self, size: int = 1) -> bytes:
        return self._ser.read(size)

    def set_timeout(self, timeout: float):
        self._ser.timeout = timeout

    def reset_input_buffer(self):
        try:
            self._ser.reset_input_buffer()
        except Exception:
            pass

    def reset_output_buffer(self):
        try:
            self._ser.reset_output_buffer()
        except Exception:
            pass

    def close(self):
        try:
            self._ser.close()
        except Exception:
            pass


class MockTransport(TransportBase):
    """Simple mock transport for unit tests and mock-device runs.

    If auto_respond is True, the transport will synthesize ACK/status/version
    frames so higher-level code can exercise the full driver without hardware.
    """

    def __init__(self, auto_respond: bool = False, version: tuple[int, int, int] = (0, 0, 1)):
        self._write_log = []
        self._resp = bytearray()
        self._timeout = 1.0
        self._auto = auto_respond
        self._version = version

    def queue_response(self, data: bytes):
        self._resp.extend(data)

    def write(self, data: bytes) -> int:
        self._write_log.append(bytes(data))

        if self._auto and data:
            opcode = data[0]
            # VERSION request (0xFF) -> respond with version bytes only (no ACK)
            if opcode == 0xFF:
                self.queue_response(bytes(self._version))
            # INIT (0x24) -> ACK + quick completion status
            elif opcode == 0x24:
                self.queue_response(b"\x09")
                self.queue_response(b"\xff\xff\x00\x00")
                self.queue_response(b"\xff\xff\x00\x64")
            # STOP (0x16) -> no response (driver doesn't read it, see connect_transport comment)
            elif opcode == 0x16:
                pass  # Driver writes STOP but doesn't read response
            # ACK-required commands (CONNECT, HOME, BOUNDS, FRAMING, DATA, JOB_HEADER, etc)
            elif opcode in (0x0A, 0x17, 0x20, 0x21, 0x22, 0x23, 0x25, 0x28):
                self.queue_response(bytes([0x09]))
            # Other commands -> heartbeat
            else:
                self.queue_response(b"\xff\xff\xff\xfe")

        return len(data)

    def read(self, size: int = 1) -> bytes:
        if not self._resp:
            return b""
        out = bytes(self._resp[:size])
        del self._resp[:size]
        return out

    def set_timeout(self, timeout: float):
        self._timeout = timeout

    def reset_input_buffer(self):
        self._resp = bytearray()

    def reset_output_buffer(self):
        self._write_log = []

    def close(self):
        self._resp = bytearray()
        self._write_log = []

    @property
    def writes(self):
        return list(self._write_log)
