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
    """Simple mock transport for unit tests.

    Usage:
        m = MockTransport()
        m.queue_response(b"\xff\xff\xff\xfe")
        m.queue_response(b"\x09")
        m.write(b"\x0a\x00\x04\x00")
        b = m.read(1)
    """

    def __init__(self):
        self._write_log = []
        self._resp = bytearray()
        self._timeout = 1.0

    def queue_response(self, data: bytes):
        self._resp.extend(data)

    def write(self, data: bytes) -> int:
        self._write_log.append(bytes(data))
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
