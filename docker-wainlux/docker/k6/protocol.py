"""K6 protocol utilities: checksum, packet builders, and minimal send/receive logic.

This module contains pure helpers and a `send_cmd` helper that works with
TransportBase objects (see `transport.py`). The goal is readable, testable
code that mirrors observed behavior in `k6_burn_image.py`.
"""

from __future__ import annotations
import time
from typing import Tuple

ACK = 0x09
HEARTBEAT = b"\xff\xff\xff\xfe"
STATUS_PREFIX = b"\xff\xff\x00"
DATA_CHUNK = 1900


def checksum(packet: bytes) -> int:
    """Two's complement (8-bit) checksum matching observed behavior.

    Equivalent to: (-sum(packet[:-1])) & 0xFF
    The packet passed should be the entire packet with space for checksum
    (last byte may be zero), or we sum the bytes except the last placeholder.
    """
    total = sum(packet[:-1]) if len(packet) >= 1 else 0
    return (-total) & 0xFF


def build_data_packet(payload: bytes) -> bytes:
    """Build a DATA (0x22) packet with length and checksum.

    Layout:
      0: opcode 0x22
      1-2: length (big-endian)
      3..N-2: payload
      N-1: checksum
    """
    if len(payload) > DATA_CHUNK:
        raise ValueError("Payload too large")

    packet_len = len(payload) + 4
    pkt = bytearray(packet_len)
    pkt[0] = 0x22
    pkt[1] = (packet_len >> 8) & 0xFF
    pkt[2] = packet_len & 0xFF
    pkt[3 : 3 + len(payload)] = payload
    pkt[-1] = checksum(pkt)
    return bytes(pkt)


def build_job_header_raster(
    width: int, height: int, depth: int, power: int = 1000, center_x: int = None, center_y: int = None
) -> bytes:
    """Build JOB_HEADER (0x23) for raster-only burn (no vector).

    Args:
        width: Raster width in pixels
        height: Raster height in pixels
        depth: Burn depth 1-255
        power: Laser power 0-1000 (default 1000)
        center_x: Center X coordinate (default: width/2 + 67)
        center_y: Center Y coordinate (default: 800 = centered in 1600px work area)

    Returns:
        38-byte JOB_HEADER packet
    """
    # Calculate total payload size (packed pixels)
    bytes_per_line = (width + 7) // 8
    total_size = bytes_per_line * height

    # Center point calculation - default to center of 1600x1520 work area (76mm actual)
    if center_x is None:
        center_x = (width // 2) + 67
    if center_y is None:
        center_y = 760  # Center of 1520px work area (76mm measured Y-axis)

    return build_job_header_custom(
        raster_w=width,
        raster_h=height,
        raster_power=power,
        raster_depth=depth,
        vector_w=0,
        vector_h=0,
        total_size=total_size,
        vector_power=0,
        vector_depth=0,
        point_count=0,
        center_x=center_x,
        center_y=center_y,
        quality=1,
    )


def build_bounds_packet(
    width: int, height: int, center_x: int = None, center_y: int = None
) -> bytes:
    """Build BOUNDS (0x20) packet for preview rectangle.

    Args:
        width: Rectangle width in pixels
        height: Rectangle height in pixels
        center_x: Center X coordinate (default: width/2 + 67, matching JOB_HEADER)
        center_y: Center Y coordinate (default: height/2)

    Returns:
        11-byte packet: opcode + dimensions + center position
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

    return bytes(pkt)


def build_job_header_custom(
    raster_w: int,
    raster_h: int,
    raster_power: int,
    raster_depth: int,
    vector_w: int,
    vector_h: int,
    total_size: int,
    vector_power: int,
    vector_depth: int,
    point_count: int,
    center_x: int,
    center_y: int,
    quality: int = 1,
) -> bytes:
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

    # Packet count heuristic used by legacy script
    param1 = (total_size // 4094) + 1
    put_u16_be(3, param1)
    hdr[5] = 0x01

    put_u16_be(6, raster_w)
    put_u16_be(8, raster_h)
    put_u16_be(10, 33)
    put_u16_be(12, raster_power)
    put_u16_be(14, raster_depth)
    put_u16_be(16, vector_w)
    put_u16_be(18, vector_h)
    put_u32_be(20, total_size)
    put_u16_be(24, vector_power)
    put_u16_be(26, vector_depth)
    put_u32_be(28, point_count)
    put_u16_be(32, center_x)
    put_u16_be(34, center_y)
    hdr[36] = quality
    hdr[37] = 0x00

    return bytes(hdr)


def parse_response_frames(rx: bytes) -> Tuple[int, int, str]:
    """Analyze rx bytes: return (heartbeat_count, ack_count, response_type_str).

    response_type_str is one of: 'NONE','HEARTBEAT','ACK','HEARTBEAT+ACK','OTHER'
    """
    if not rx:
        return 0, 0, "NONE"

    hb = 0
    for i in range(0, max(0, len(rx) - 3)):
        if rx[i : i + 4] == HEARTBEAT:
            hb += 1

    ack = rx.count(bytes([ACK]))

    if hb and ack:
        typ = "HEARTBEAT+ACK"
    elif hb:
        typ = "HEARTBEAT"
    elif ack:
        typ = "ACK"
    else:
        typ = "OTHER"
    return hb, ack, typ


def send_cmd(
    transport,
    name: str,
    data: bytes,
    timeout: float = 2.0,
    expect_ack: bool = True,
    min_response_len: int = 0,
    csv_logger=None,
    phase: str = "operation",
) -> bytes:
    """Send data over transport and wait for response.

    Returns raw bytes read. This is intentionally small and testable.

    Args:
        min_response_len: if >0, return as soon as at least this many bytes
                          have been received (useful for VERSION which returns 3 bytes).
        csv_logger: Optional CSVLogger instance for logging operation
        phase: Phase name for CSV logging (connect, burn, wait, etc.)
    """
    t_start = time.time()
    # Do not clear input buffer here (tests may queue responses); caller should
    # clear stale buffers when appropriate.
    transport.set_timeout(timeout)

    try:
        transport.write(data)
    except Exception:  # pragma: no cover - depends on serial
        raise

    rx = bytearray()
    deadline = time.time() + timeout

    while time.time() < deadline:
        # read one byte
        b = transport.read(1)
        if b:
            rx.extend(b)
            # If we have a minimum response length configured, stop once it is reached
            if min_response_len and len(rx) >= min_response_len:
                break
            # short-circuit: if we see ACK and we're expecting it, stop
            if expect_ack and ACK in rx:
                # Stop on ACK immediately; do not consume further queued responses.
                break
            # If not expecting ACK, accept heartbeat OR status frame (4 bytes each)
            if not expect_ack and len(rx) >= 4:
                # HEARTBEAT: FF FF FF FE
                if rx[-4:] == HEARTBEAT:
                    break
                # STATUS: FF FF 00 XX (where XX is 0-100 percent)
                if rx[-4] == 0xFF and rx[-3] == 0xFF and rx[-2] == 0x00:
                    break
        else:
            # avoid busy spin
            time.sleep(0.01)

    # Log operation if csv_logger provided
    if csv_logger:
        duration_ms = (time.time() - t_start) * 1000
        hb, ack, response_type = parse_response_frames(rx)
        csv_logger.log_operation(
            phase=phase,
            operation=name,
            duration_ms=duration_ms,
            bytes_transferred=len(data) + len(rx),
            response_type=response_type,
            state="COMPLETE" if rx else "TIMEOUT",
        )

    return bytes(rx)


# Exceptions for protocol-level errors
class K6Error(Exception):
    """Base class for K6 protocol errors."""


class K6TimeoutError(K6Error):
    """Raised when an operation times out."""


class K6DeviceError(K6Error):
    """Raised when device responds with error or negative condition."""


def send_cmd_checked(
    transport,
    name: str,
    data: bytes,
    timeout: float = 2.0,
    expect_ack: bool = True,
    csv_logger=None,
    phase: str = "operation",
):
    """Send a command and raise on timeouts or missing ACKs.

    Returns raw bytes on success. Raises K6TimeoutError if no response, or
    K6DeviceError if an ACK was expected but not present.

    Args:
        csv_logger: Optional CSVLogger instance for logging
        phase: Phase name for CSV logging
    """
    rx = send_cmd(
        transport,
        name,
        data,
        timeout=timeout,
        expect_ack=expect_ack,
        csv_logger=csv_logger,
        phase=phase,
    )

    if not rx:
        raise K6TimeoutError(f"Timeout waiting for response to {name}")

    hb, ack, typ = parse_response_frames(rx)

    if expect_ack and ack == 0:
        # No ACK where one was expected
        raise K6DeviceError(f"No ACK for {name} (response: {rx.hex()})")

    return rx


def wait_for_completion(
    transport, max_wait_s: float, idle_s: float = 90.0, csv_logger=None
):
    """Wait for FF FF 00 XX status frames and report completion.

    Behaviour:
      - On 100% status: return {'status': 'complete', 'total_time': seconds}
      - On idle timeout (no data for idle_s):
        return {'status': 'idle_timeout', 'last_pct': last_pct}
      - On max_wait exceeded: raise K6TimeoutError

    The function reads bytes from transport and looks for the status prefix
    `FF FF 00 XX` where XX is progress percentage.

    Args:
        csv_logger: Optional CSVLogger instance for logging status updates
    """
    start = time.time()
    last_rx = time.time()
    buf = bytearray()
    last_pct = None

    while True:
        if time.time() - start > max_wait_s:
            raise K6TimeoutError("Max wait exceeded waiting for completion")

        b = transport.read(1)
        if b:
            last_rx = time.time()
            buf.append(b[0])
            if len(buf) > 16:
                buf = buf[-16:]

            # Look for status frame: FF FF 00 XX
            if (
                len(buf) >= 4
                and buf[-4] == 0xFF
                and buf[-3] == 0xFF
                and buf[-2] == 0x00
            ):
                pct = buf[-1]
                time.time()

                if pct != last_pct:
                    last_pct = pct

                    # Log status update if csv_logger provided
                    if csv_logger:
                        csv_logger.log_operation(
                            phase="wait",
                            operation="STATUS",
                            duration_ms=0,
                            status_pct=pct,
                            response_type="STATUS",
                            device_state="BURNING" if pct < 100 else "COMPLETE",
                        )

                    if pct == 100:
                        total_time = time.time() - start
                        return {"status": "complete", "total_time": total_time}
        else:
            if time.time() - last_rx > idle_s:
                return {"status": "idle_timeout", "last_pct": last_pct}
            # small sleep to avoid busy loop
            time.sleep(0.01)


def burn_payload(
    transport, payload_lines: list[bytes], max_retries: int = 3, csv_logger=None
) -> int:
    """Send payload lines with chunking and retry logic.

    Args:
        transport: TransportBase instance
        payload_lines: List of packed pixel bytes (one per line)
        max_retries: Max attempts per chunk before raising
        csv_logger: Optional CSVLogger instance for logging chunks and retries

    Returns:
        Total chunks sent

    Raises:
        K6DeviceError: If chunk fails after max_retries

    Strategy:
        - Break each line into DATA_CHUNK (1900 byte) pieces
        - Send each chunk with DATA (0x22) packet
        - Retry with exponential backoff on failure
        - Fail fast on max retries exceeded
    """
    chunk_count = 0

    # Calculate total chunks for progress tracking
    total_chunks = sum(
        (len(line) + DATA_CHUNK - 1) // DATA_CHUNK for line in payload_lines
    )

    for line_idx, line in enumerate(payload_lines):
        offset = 0
        while offset < len(line):
            chunk = line[offset : offset + DATA_CHUNK]
            attempt = 0

            while attempt < max_retries:
                try:
                    pkt = build_data_packet(chunk)
                    send_cmd_checked(
                        transport,
                        f"DATA chunk {chunk_count + 1}/{total_chunks} (line {line_idx})",
                        pkt,
                        timeout=2.0,
                        expect_ack=True,
                        csv_logger=csv_logger,
                        phase="burn",
                    )
                    chunk_count += 1
                    break  # success

                except (K6TimeoutError, K6DeviceError) as e:
                    attempt += 1

                    # Log retry if csv_logger provided
                    if csv_logger:
                        csv_logger.log_operation(
                            phase="burn",
                            operation=f"DATA line {line_idx} RETRY",
                            duration_ms=0,
                            bytes_transferred=0,
                            retry_count=attempt,
                            state="RETRY",
                            device_state="ERROR",
                        )

                    if attempt >= max_retries:
                        raise K6DeviceError(
                            f"Chunk failed after {max_retries} attempts "
                            f"(line {line_idx}, offset {offset}): {e}"
                        )

                    # Exponential backoff: 0.1s, 0.2s, 0.4s...
                    backoff = 0.1 * (2 ** (attempt - 1))
                    time.sleep(backoff)

            offset += DATA_CHUNK

    return chunk_count
