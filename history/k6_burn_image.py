#!/usr/bin/env python3
"""
K6 Image Burn - Java protocol implementation
VERSION → CONNECT → HOME → FRAMING → INIT (x2) → JOB_HEADER → DATA (0x22 chunks)
"""

import serial
import time
import math
import csv
import json
import sys
import traceback
from datetime import datetime
from PIL import Image, ImageOps, ImageDraw
import argparse

# Constants
DATA_CHUNK = 1900
ACK = 0x09

# Global CSV writer and error logger
csv_writer = None
csv_cumulative_bytes = 0
csv_start_time = None
error_log = None
verbose_mode = False
csv_start_time = None


def timestamp():
    """Return current timestamp for logging"""
    return datetime.now().strftime("%H:%M:%S.%f")[:-3]


def log_error(message, exception=None):
    """Log errors to both console and error log file"""
    global error_log
    timestamp_str = timestamp()
    error_msg = f"[{timestamp_str}] ERROR: {message}"
    if exception:
        error_msg += f"\n{traceback.format_exc()}"

    print(error_msg, file=sys.stderr)
    if error_log:
        error_log.write(error_msg + "\n")
        error_log.flush()


def log_verbose(message):
    """Log verbose debug information"""
    global verbose_mode
    if verbose_mode:
        print(f"[{timestamp()}] VERBOSE: {message}")


def log_csv(
    phase,
    operation,
    duration_ms,
    bytes_transferred=0,
    status_pct=None,
    state="ACTIVE",
    response_type="",
    retry_count=0,
    device_state="IDLE",
):
    """Log operation to CSV if enabled

    Args:
        phase: SETUP, BUILD, DATA, BURN
        operation: Specific command/action name
        duration_ms: Time taken
        bytes_transferred: Bytes sent/received
        status_pct: Burn progress 0-100
        state: HIGH_LEVEL_STATE for timing diagrams (INIT, SETUP, UPLOAD_BUILD, UPLOAD_TX, BURN_RASTER, BURN_VECTOR, WAIT, COMPLETE)
        response_type: Device response (ACK, HEARTBEAT, STATUS, TIMEOUT, NONE)
        retry_count: Number of retries for this operation
        device_state: Device state (IDLE, BUSY, HEARTBEAT, BURNING)
    """
    global csv_writer, csv_cumulative_bytes, csv_start_time
    if csv_writer is None:
        return

    csv_cumulative_bytes += bytes_transferred
    throughput_kbps = (
        (bytes_transferred / 1024) / (duration_ms / 1000) if duration_ms > 0 else 0
    )
    elapsed_s = (time.time() - csv_start_time) if csv_start_time else 0

    # Get burn start time from first row (will be set in main)
    burn_start = (
        datetime.fromtimestamp(csv_start_time).strftime("%Y-%m-%d %H:%M:%S")
        if csv_start_time
        else ""
    )

    csv_writer.writerow(
        [
            burn_start,
            timestamp(),
            f"{elapsed_s:.3f}",
            phase,
            operation,
            f"{duration_ms:.0f}",
            bytes_transferred,
            csv_cumulative_bytes,
            f"{throughput_kbps:.2f}",
            status_pct if status_pct is not None else "",
            state,
            response_type,
            retry_count,
            device_state,
        ]
    )


def checksum(packet: bytes) -> int:
    """Two's complement checksum (8-bit)"""
    total = sum(packet[:-1])
    if total > 0xFF:
        return ((~total) + 1) & 0xFF
    return total & 0xFF


def send_cmd(
    ser, name, data, timeout=2.0, csv_phase="SETUP", csv_bytes=0, expect_ack=True
):
    """Send command, read response. Handles device status frames.

    Status frames from device:
    - FF FF FF FE: Heartbeat while processing (every ~4s, no ACK follows)
    - FF FF 00 XX: Burn progress (XX = 0-100 percent)
    - 09: ACK
    """
    t_start = time.time()
    log_verbose(f"TX {name}: {data[:40].hex()}{'...' if len(data) > 40 else ''}")
    print(f"[{timestamp()}] {name}: {data[:20].hex()}{'...' if len(data) > 20 else ''}")

    try:
        ser.timeout = timeout
        ser.write(data)
    except Exception as e:
        log_error(f"Serial write failed for {name}", e)
        raise

    rx = bytearray()
    deadline = time.time() + timeout
    heartbeat_count = 0

    # Read bytes until we get ACK or timeout (or get first heartbeat if not expecting ACK)
    while time.time() < deadline:
        ser.timeout = min(0.5, deadline - time.time())
        try:
            byte = ser.read(1)
        except Exception as e:
            log_error(f"Serial read failed for {name}", e)
            raise

        if byte:
            rx.extend(byte)
            # If we got ACK, read any remaining buffered bytes and return
            if byte[0] == ACK:
                ser.timeout = 0.01
                extra = ser.read(100)
                if extra:
                    rx.extend(extra)
                break
            # If not expecting ACK and we got first heartbeat (4 bytes), we're done
            if not expect_ack and len(rx) >= 4:
                if rx[:4] == b"\xff\xff\xff\xfe":
                    break
        else:
            time.sleep(0.01)

    elapsed = (time.time() - t_start) * 1000

    # Analyze response - count heartbeat frames (FF FF FF FE pattern)
    rx_bytes = bytes(rx)

    if verbose_mode and rx_bytes:
        log_verbose(f"RX {name} full dump: {rx_bytes.hex()}")

    i = 0
    while i <= len(rx_bytes) - 4:
        if rx_bytes[i : i + 4] == b"\xff\xff\xff\xfe":
            heartbeat_count += 1
            i += 4
        else:
            i += 1

    has_ack = ACK in rx_bytes

    # Determine response type for timing diagram
    if not rx_bytes:
        response_type = "TIMEOUT"
        log_error(
            f"Timeout waiting for response to {name} (expected: {'ACK' if expect_ack else 'HEARTBEAT'})"
        )
    elif heartbeat_count > 0 and has_ack:
        response_type = "HEARTBEAT+ACK"
    elif heartbeat_count > 0:
        response_type = "HEARTBEAT"
    elif has_ack:
        response_type = "ACK"
    else:
        response_type = "OTHER"
        if verbose_mode:
            log_verbose(f"Unexpected response type for {name}: {rx_bytes.hex()}")

    if rx_bytes:
        parts = []
        if heartbeat_count:
            parts.append(
                f"{heartbeat_count} heartbeat{'s' if heartbeat_count > 1 else ''}"
            )
        if has_ack:
            parts.append("ACK")
        msg = f" ({', '.join(parts)})" if parts else ""
        print(f"[{timestamp()}]   RX ({elapsed:.0f}ms): {rx_bytes.hex()}{msg}")
    else:
        print(f"[{timestamp()}]   RX ({elapsed:.0f}ms): TIMEOUT")

    # Determine state and device_state based on phase and response
    if csv_phase == "SETUP":
        state = "SETUP"
    elif csv_phase == "BUILD":
        state = "UPLOAD_BUILD"
    elif csv_phase == "DATA":
        state = "UPLOAD_TX"
    elif csv_phase == "BURN":
        state = "BURN"
    else:
        state = "ACTIVE"

    # Determine device state based on response
    if not rx_bytes:
        device_state = "TIMEOUT"
    elif heartbeat_count > 0:
        device_state = "HEARTBEAT"
    elif has_ack:
        device_state = "ACK"
    else:
        device_state = "UNKNOWN"

    # Log to CSV with retry count (0 for successful first attempt)
    log_csv(
        state,
        name,
        elapsed,
        len(data),
        response_type,
        retry_count=0,
        device_state=device_state,
    )

    return rx_bytes


def init_k6(ser):
    """K6 initialization: STOP → VERSION → CONNECT (x2) → HOME"""
    # Flush any stale data
    ser.reset_input_buffer()
    ser.reset_output_buffer()

    # STOP any previous job
    send_cmd(ser, "STOP", bytes([0x16, 0x00, 0x04, 0x00]))

    # VERSION
    rx = send_cmd(ser, "VERSION", bytes([0xFF, 0x00, 0x04, 0x00]))
    if len(rx) == 3:
        print(f"[{timestamp()}]   Device: {rx[0]}.{rx[1]}.{rx[2]}")

    # CONNECT (twice)
    for i in range(2):
        rx = send_cmd(ser, f"CONNECT #{i+1}", bytes([0x0A, 0x00, 0x04, 0x00]))
        if ACK not in rx:
            print(f"[{timestamp()}]   WARNING: No ACK for CONNECT #{i+1}: {rx.hex()}")

    # HOME
    rx = send_cmd(ser, "HOME", bytes([0x17, 0x00, 0x04, 0x00]), timeout=10.0)
    if ACK not in rx:
        raise RuntimeError("HOME failed")
    print("  Homing...")


def send_job_header_custom(
    ser,
    raster_w,
    raster_h,
    raster_power,
    raster_depth,
    vector_w,
    vector_h,
    total_size,
    vector_power,
    vector_depth,
    point_count,
    center_x,
    center_y,
    quality=1,
):
    """Send JOB_HEADER (0x23, 38 bytes) with raster + vector fields.

    CRITICAL: Uses BIG ENDIAN
    """
    param1 = (total_size // 4094) + 1

    hdr = bytearray(38)
    hdr[0] = 0x23  # JOB_HEADER opcode
    hdr[1] = 0x00
    hdr[2] = 38  # Length

    def put_u16_be(idx, val):
        hdr[idx] = (val >> 8) & 0xFF
        hdr[idx + 1] = val & 0xFF

    def put_u32_be(idx, val):
        hdr[idx] = (val >> 24) & 0xFF
        hdr[idx + 1] = (val >> 16) & 0xFF
        hdr[idx + 2] = (val >> 8) & 0xFF
        hdr[idx + 3] = val & 0xFF

    put_u16_be(3, param1)
    hdr[5] = 0x01
    put_u16_be(6, raster_w)
    put_u16_be(8, raster_h)
    put_u16_be(10, 33)  # raster offset
    put_u16_be(12, raster_power)
    put_u16_be(14, raster_depth)
    put_u16_be(16, vector_w)
    put_u16_be(18, vector_h)
    put_u32_be(20, total_size)  # total size (33 + raster + vector)
    put_u16_be(24, vector_power)
    put_u16_be(26, vector_depth)
    put_u32_be(28, point_count)
    put_u16_be(32, center_x)
    put_u16_be(34, center_y)
    hdr[36] = quality
    hdr[37] = 0x00

    # JOB_HEADER sends continuous heartbeats, wait for first one (~5s)
    send_cmd(
        ser, "JOB_HEADER", bytes(hdr), timeout=10.0, csv_phase="SETUP", expect_ack=False
    )
    return param1


def send_job_header(ser, width, height, power, depth=10):
    """Send raster-only JOB_HEADER."""
    width_bytes = (width + 7) // 8
    payload_size = height * width_bytes
    total_size = 33 + payload_size
    center_x = (width // 2) + 67
    center_y = height // 2
    return send_job_header_custom(
        ser,
        width,
        height,
        power,
        depth,
        0,
        0,
        total_size,
        power,
        0,
        0,
        center_x,
        center_y,
        quality=1,
    )


def process_image(path, max_width=1600, threshold=128, margin=5):
    """Load and convert image to 1-bit.

    K6 work area nominally 1600x1520. Default max_width=1600.
    """
    img = Image.open(path)
    img.thumbnail((max_width, max_width), Image.Resampling.LANCZOS)
    img = img.convert("L")
    img = img.point(lambda x: 255 if x > threshold else 0, "1")

    # crop to content bounds (+~5px margin) to avoid out-of-bounds.
    inv = ImageOps.invert(img.convert("L"))
    bbox = inv.getbbox()
    if bbox:
        left = max(bbox[0] - margin, 0)
        upper = max(bbox[1] - margin, 0)
        right = min(bbox[2] + margin, img.width)
        lower = min(bbox[3] + margin, img.height)
        img = img.crop((left, upper, right, lower))

    return img


def build_test_pattern(
    pattern, max_px=1600, mm_per_px=0.05, margin_px=2, circle_mm=20, boundary_mm=80
):
    """Create simple test patterns in pixel space.

    pattern:
      - boundary: outline the full work area
      - circle: centered circle with given diameter
      - both: boundary + circle
    """
    boundary_px = min(max_px, max(1, int(round(boundary_mm / mm_per_px))))
    img = Image.new("1", (boundary_px, boundary_px), 1)  # 1=white
    draw = ImageDraw.Draw(img)

    if pattern in ("boundary", "both"):
        draw.rectangle(
            (
                margin_px,
                margin_px,
                boundary_px - 1 - margin_px,
                boundary_px - 1 - margin_px,
            ),
            outline=0,
        )

    if pattern in ("circle", "both"):
        diameter_px = max(1, int(round(circle_mm / mm_per_px)))
        radius = diameter_px // 2
        cx = boundary_px // 2
        cy = boundary_px // 2
        draw.ellipse((cx - radius, cy - radius, cx + radius, cy + radius), outline=0)

    return img


def build_raster_payload_chunked(img, chunk_size=DATA_CHUNK):
    """Generator: yields raster payload in chunks (0xFF = black/laser on)"""
    width, height = img.size
    width_bytes = (width + 7) // 8
    width_bytes * height

    chunk_buffer = bytearray()
    chunk_count = 0
    t_chunk_start = time.time()

    for y in range(height):
        for x in range(0, width, 8):
            byte_val = 0
            for bit in range(8):
                if x + bit < width:
                    pixel = img.getpixel((x + bit, y))
                    if pixel == 0:  # Black = laser on
                        byte_val |= 0x80 >> bit
            chunk_buffer.append(byte_val)

            # Yield full chunks
            if len(chunk_buffer) >= chunk_size:
                chunk_time = (time.time() - t_chunk_start) * 1000
                print(
                    f"[{timestamp()}]   Built chunk {chunk_count}: {chunk_time:.0f}ms"
                )
                log_csv(
                    "BUILD",
                    f"CHUNK_{chunk_count}",
                    chunk_time,
                    chunk_size,
                    state="UPLOAD_BUILD",
                )
                yield bytes(chunk_buffer[:chunk_size])
                chunk_buffer = chunk_buffer[chunk_size:]
                chunk_count += 1
                t_chunk_start = time.time()

    # Yield remaining bytes
    if chunk_buffer:
        chunk_time = (time.time() - t_chunk_start) * 1000
        print(
            f"[{timestamp()}]   Built chunk {chunk_count} (final): {chunk_time:.0f}ms"
        )
        log_csv(
            "BUILD",
            f"CHUNK_{chunk_count}_FINAL",
            chunk_time,
            len(chunk_buffer),
            state="UPLOAD_BUILD",
        )
        yield bytes(chunk_buffer)


def build_vector_circle_points(diameter_mm, mm_per_px=0.05):
    """Generate vector points for a circle outline."""
    radius_px = max(1, int(round((diameter_mm / 2) / mm_per_px)))
    count = max(36, int(2 * 3.14159 * radius_px))
    points = set()
    for i in range(count):
        angle = 2 * 3.14159 * (i / count)
        x = int(round(radius_px + radius_px * math.cos(angle)))
        y = int(round(radius_px + radius_px * math.sin(angle)))
        points.add((x, y))
    pts = sorted(points)
    return pts


def build_vector_payload(points):
    """Build vector payload (x,y little-endian)."""
    payload = bytearray(len(points) * 4)
    idx = 0
    for x, y in points:
        payload[idx] = x & 0xFF
        payload[idx + 1] = (x >> 8) & 0xFF
        payload[idx + 2] = y & 0xFF
        payload[idx + 3] = (y >> 8) & 0xFF
        idx += 4
    return bytes(payload)


def build_data_packet(payload: bytes) -> bytes:
    """Build DATA packet (0x22) with checksum - NO PADDING"""
    if len(payload) > DATA_CHUNK:
        raise ValueError(f"Payload too large: {len(payload)} > {DATA_CHUNK}")

    # Send exact payload size - no padding on last chunk
    packet_len = len(payload) + 4

    pkt = bytearray(packet_len)
    pkt[0] = 0x22  # DATA opcode
    pkt[1] = (packet_len >> 8) & 0xFF
    pkt[2] = packet_len & 0xFF
    pkt[3 : 3 + len(payload)] = payload
    pkt[-1] = checksum(pkt)

    return bytes(pkt)


def burn_payload(
    ser,
    payload_or_gen,
    total_bytes,
    packet_count,
    wait_lines,
    wait_per_line=0.6,
    min_wait=120,
    no_burn=False,
):
    """Send payload via DATA chunks (accepts bytes or generator) and wait for completion."""
    print(
        f"[{timestamp()}]   Streaming payload: {total_bytes} bytes, {packet_count} chunks"
    )

    # Handle both generator and pre-built payload
    if hasattr(payload_or_gen, "__iter__") and not isinstance(
        payload_or_gen, (bytes, bytearray)
    ):
        chunk_gen = payload_or_gen
    else:
        # Legacy: slice pre-built payload
        def slice_gen():
            offset = 0
            while offset < len(payload_or_gen):
                yield payload_or_gen[offset : offset + DATA_CHUNK]
                offset += DATA_CHUNK

        chunk_gen = slice_gen()

    offset = 0
    chunk_num = 0
    t_start = time.time()
    for chunk in chunk_gen:
        data_pkt = build_data_packet(chunk)

        retries = 0
        while retries < 5:
            rx = send_cmd(
                ser,
                f"DATA chunk {chunk_num}",
                data_pkt,
                timeout=0.5,
                csv_phase="DATA",
                csv_bytes=len(chunk),
            )
            if ACK in rx:
                offset += len(chunk)
                chunk_num += 1
                elapsed = time.time() - t_start
                rate = (offset / 1024) / elapsed if elapsed > 0 else 0
                pct = min(offset, total_bytes) * 100 // total_bytes
                print(
                    f"[{timestamp()}]   Progress: {min(offset, total_bytes)}/{total_bytes} bytes ({pct}%, {rate:.1f} KB/s)"
                )
                # Small delay after successful chunk to let device process
                time.sleep(0.05)
                break
            retries += 1
            if verbose_mode:
                log_verbose(f"Retry {retries}/5 for chunk {chunk_num}")
            time.sleep(0.2)

        if retries >= 5:
            log_error(f"No ACK for chunk {chunk_num} after {retries} retries")
            send_cmd(ser, "STOP", bytes([0x16, 0x00, 0x04, 0x00]), csv_phase="DATA")
            return False

        # Log retry count to CSV if there were retries
        if retries > 0:
            log_csv(
                "DATA",
                f"CHUNK_{chunk_num}_RETRIES",
                0,
                len(chunk),
                "RETRY",
                retry_count=retries,
                device_state="RETRY",
            )

    if no_burn:
        print("--no-burn: Skipping POST-INITs and burn trigger")
        return True

    init_cmd = bytes([0x24, 0x00, 0x0B, 0x00] + [0x00] * 7)
    send_cmd(ser, "INIT post-data #1", init_cmd, csv_phase="DATA")
    send_cmd(ser, "INIT post-data #2", init_cmd, csv_phase="DATA")

    print("Burn started - waiting for completion...")
    est_time = max(wait_lines * wait_per_line, min_wait)
    # Use 5x the estimate as max timeout - actual burns often take much longer than estimate
    # Idle timeout (90s) will catch actual completion
    max_wait = est_time * 5
    print(f"  Estimated burn time: {est_time:.1f}s (max timeout: {max_wait:.1f}s)")

    # Reset serial timeout for burn monitoring (send_cmd may have left it at 0.01s)
    ser.timeout = 1.0

    wait_for_completion(ser, max_wait)
    print("Burn sequence complete")
    return True


def wait_for_completion(ser, max_wait_s, idle_s=90):
    """Watch for FF FF 00 XX status frames and report progress.

    Exit conditions (in priority order):
    1. See 100% status → COMPLETE_100%
    2. No data for idle_s seconds → IDLE_TIMEOUT
    3. Total time exceeds max_wait_s → MAX_TIMEOUT (safety fallback)

    TODO: Verify actual burn time for a full 1600px line at max depth (255) and max power (1000).
          Current idle_s=90 is conservative based on observed gaps of up to 15s between status updates.
          Device can have long pauses between status updates during complex burn sections.
    """
    start = time.time()
    last_rx = time.time()
    last_status_time = None
    buf = []
    last_pct = None

    while True:
        # Safety fallback - should rarely hit with 3x multiplier
        if time.time() - start > max_wait_s:
            total_time = time.time() - start
            print(
                f"[{timestamp()}]   WARNING: Max timeout reached after {total_time:.1f}s (last status: {last_pct}%)"
            )
            log_csv(
                "BURN",
                "MAX_TIMEOUT",
                total_time * 1000,
                0,
                status_pct=last_pct,
                state="TIMEOUT",
                response_type="MAX_TIMEOUT",
                retry_count=0,
                device_state="TIMEOUT",
            )
            return

        b = ser.read(1)
        if b:
            last_rx = time.time()
            buf.append(b[0])
            if len(buf) > 16:
                buf = buf[-16:]

            # Look for ... FF FF 00 XX
            if (
                len(buf) >= 4
                and buf[-4] == 0xFF
                and buf[-3] == 0xFF
                and buf[-2] == 0x00
            ):
                pct = buf[-1]
                current_time = time.time()

                if pct != last_pct:
                    if last_status_time is not None:
                        delta = (current_time - last_status_time) * 1000
                        print(
                            f"[{timestamp()}]   Status: FF FF 00 {pct:02x} → {pct}% (Δ{delta:.0f}ms)"
                        )
                        log_csv(
                            "BURN",
                            f"STATUS_{pct}%",
                            delta,
                            0,
                            status_pct=pct,
                            state="BURN",
                            response_type="STATUS",
                            retry_count=0,
                            device_state="BURNING",
                        )
                    else:
                        print(f"[{timestamp()}]   Status: FF FF 00 {pct:02x} → {pct}%")
                        log_csv(
                            "BURN",
                            f"STATUS_{pct}%",
                            0,
                            0,
                            status_pct=pct,
                            state="BURN",
                            response_type="STATUS",
                            retry_count=0,
                            device_state="BURNING",
                        )
                    last_pct = pct
                    last_status_time = current_time

                    # Check for 100% completion
                    if pct == 100:
                        total_time = time.time() - start
                        print(
                            f"[{timestamp()}]   Burn complete at 100% (total: {total_time:.1f}s)"
                        )
                        log_csv(
                            "BURN",
                            "COMPLETE_100%",
                            0,
                            0,
                            status_pct=100,
                            state="COMPLETE",
                            response_type="COMPLETE",
                            retry_count=0,
                            device_state="COMPLETE",
                        )
                        return
        else:
            # Idle timeout - normal completion when device stops sending data
            if time.time() - last_rx > idle_s:
                elapsed_idle = (time.time() - last_rx) * 1000
                total_time = time.time() - start
                print(
                    f"[{timestamp()}]   Burn complete: idle {elapsed_idle:.0f}ms > {idle_s}s threshold (last status: {last_pct}%, total: {total_time:.1f}s)"
                )
                log_csv(
                    "BURN",
                    "IDLE_TIMEOUT",
                    elapsed_idle,
                    0,
                    status_pct=last_pct,
                    state="COMPLETE",
                    response_type="IDLE",
                    retry_count=0,
                    device_state="IDLE",
                )
                return


def burn_image(
    ser, img, power=1000, depth=10, wait_per_line=0.6, min_wait=120, no_burn=False
):
    """Burn image via DATA chunks (0x22)."""
    width, height = img.size
    print(f"Burning {width}x{height} @ power={power} depth={depth}")

    rx = send_cmd(ser, "FRAMING", bytes([0x21, 0x00, 0x04, 0x00]), csv_phase="SETUP")
    if ACK not in rx:
        print("  WARNING: No ACK for FRAMING")

    packet_count = send_job_header(ser, width, height, power, depth)

    for i in range(2):
        rx = send_cmd(
            ser,
            f"CONNECT post-header #{i+1}",
            bytes([0x0A, 0x00, 0x04, 0x00]),
            timeout=2.0,
            csv_phase="SETUP",
        )
        if ACK not in rx:
            print(f"  WARNING: No ACK for post-header CONNECT #{i+1}")

    # Stream payload generation
    width_bytes = (width + 7) // 8
    total_bytes = height * width_bytes
    chunk_gen = build_raster_payload_chunked(img)
    return burn_payload(
        ser,
        chunk_gen,
        total_bytes,
        packet_count,
        height,
        wait_per_line,
        min_wait,
        no_burn,
    )


def burn_vector_circle(
    ser,
    diameter_mm=20,
    power=800,
    depth=10,
    mm_per_px=0.05,
    wait_per_line=0.2,
    min_wait=60,
    no_burn=False,
):
    """Vector-only circle burn (points list)."""
    points = build_vector_circle_points(diameter_mm, mm_per_px)
    if not points:
        raise RuntimeError("No vector points generated")

    xs = [p[0] for p in points]
    ys = [p[1] for p in points]
    min_x, max_x = min(xs), max(xs)
    min_y, max_y = min(ys), max(ys)

    # Normalize to 0,0
    norm_points = [(x - min_x, y - min_y) for x, y in points]
    vector_w = (max_x - min_x) + 1
    vector_h = (max_y - min_y) + 1

    payload = build_vector_payload(norm_points)
    total_size = 33 + len(payload)
    center_x = (vector_w // 2) + 67
    center_y = vector_h // 2

    print(
        f"Vector circle {diameter_mm}mm -> {vector_w}x{vector_h} px, points={len(norm_points)}"
    )

    rx = send_cmd(ser, "FRAMING", bytes([0x21, 0x00, 0x04, 0x00]))
    if ACK not in rx:
        print("  WARNING: No ACK for FRAMING")

    packet_count = send_job_header_custom(
        ser,
        0,
        0,
        power,
        depth,
        vector_w,
        vector_h,
        total_size,
        power,
        depth,
        len(norm_points),
        center_x,
        center_y,
        quality=1,
    )

    for i in range(2):
        rx = send_cmd(
            ser,
            f"CONNECT post-header #{i+1}",
            bytes([0x0A, 0x00, 0x04, 0x00]),
            timeout=2.0,
            csv_phase="SETUP",
        )
        if ACK not in rx:
            print(f"  WARNING: No ACK for post-header CONNECT #{i+1}")

    return burn_payload(
        ser,
        payload,
        len(payload),
        packet_count,
        vector_h,
        wait_per_line,
        min_wait,
        no_burn,
    )


def main():
    parser = argparse.ArgumentParser(description="Burn image to K6")
    parser.add_argument("image", nargs="?", help="Image file")
    parser.add_argument("--port", default="/dev/ttyUSB0")
    parser.add_argument("--power", type=int, default=1000, help="Power (0-1000)")
    parser.add_argument(
        "--depth", type=int, default=10, help="Depth/intensity (default=10)"
    )
    parser.add_argument("--threshold", type=int, default=128)
    parser.add_argument(
        "--max-width", type=int, default=1600, help="Max dimension (K6 nominal 1600)"
    )
    parser.add_argument(
        "--pattern",
        choices=["boundary", "circle", "both"],
        help="Burn a test pattern instead of an image",
    )
    parser.add_argument(
        "--circle-mm",
        type=int,
        default=20,
        help="Circle diameter in mm for pattern mode",
    )
    parser.add_argument(
        "--boundary-mm",
        type=int,
        default=80,
        help="Boundary size in mm for pattern mode",
    )
    parser.add_argument(
        "--mm-per-px",
        type=float,
        default=0.05,
        help="mm per pixel for pattern size (0.05 ≈ 1600px for 80mm)",
    )
    parser.add_argument(
        "--wait-per-line",
        type=float,
        default=0.6,
        help="Seconds to wait per line after burn start",
    )
    parser.add_argument(
        "--min-wait",
        type=int,
        default=120,
        help="Minimum wait time after burn start (seconds)",
    )
    parser.add_argument(
        "--vector-circle-mm", type=int, help="Vector-only circle diameter in mm"
    )
    parser.add_argument(
        "--no-burn",
        action="store_true",
        help="Upload data but skip burn trigger (for timing tests)",
    )
    parser.add_argument(
        "--config",
        help="Load burn configuration from JSON file (replays previous burn settings)",
    )
    parser.add_argument(
        "--verbose",
        action="store_true",
        help="Enable verbose logging with full hex dumps",
    )
    args = parser.parse_args()

    # Load config if specified
    if args.config:
        print(f"Loading config from: {args.config}")
        with open(args.config, "r") as f:
            config = json.load(f)
        # Override args with config values
        for key, value in config.items():
            if key not in ["timestamp", "csv_filename", "config_filename", "burn_mode"]:
                setattr(args, key, value)
        print(f"Config loaded: {config.get('burn_mode', 'unknown')} mode")

    # Set verbose mode
    global verbose_mode
    verbose_mode = args.verbose

    # Always generate CSV stats with auto-generated filename
    global csv_writer, csv_start_time, error_log
    burn_start_time = datetime.now()
    timestamp_str = burn_start_time.strftime("%y-%m-%d-%H-%M")
    csv_filename = f"stat-{timestamp_str}.csv"
    error_log_filename = f"stat-{timestamp_str}.errors.log"
    config_filename = f"stat-{timestamp_str}.json"

    # Open error log
    error_log = open(error_log_filename, "w")
    print(f"Error log: {error_log_filename}")

    csv_file = open(csv_filename, "w", newline="")
    csv_writer = csv.writer(csv_file)
    csv_writer.writerow(
        [
            "burn_start",
            "timestamp",
            "elapsed_s",
            "phase",
            "operation",
            "duration_ms",
            "bytes_transferred",
            "cumulative_bytes",
            "throughput_kbps",
            "status_pct",
            "state",
            "response_type",
            "retry_count",
            "device_state",
        ]
    )
    csv_start_time = time.time()
    print(f"Logging to: {csv_filename}")
    print(f"Config will be saved to: {config_filename}")
    if verbose_mode:
        print("Verbose mode: ENABLED (full hex dumps)")

    if args.vector_circle_mm:
        img = None
    elif args.pattern:
        print(f"Building pattern: {args.pattern}")
        img = build_test_pattern(
            args.pattern,
            max_px=args.max_width,
            mm_per_px=args.mm_per_px,
            circle_mm=args.circle_mm,
            boundary_mm=args.boundary_mm,
        )
        print(f"Pattern size: {img.size[0]}x{img.size[1]} (1-bit)")
    else:
        if not args.image:
            raise SystemExit("Image path required unless --pattern is used")
        print(f"Loading {args.image}")
        img = process_image(args.image, args.max_width, args.threshold)
        print(f"Processed to {img.size[0]}x{img.size[1]} (1-bit)")

    print(f"Opening {args.port}")
    ser = serial.Serial(args.port, 115200, timeout=2)

    try:
        init_k6(ser)
        if args.vector_circle_mm:
            burn_vector_circle(
                ser,
                diameter_mm=args.vector_circle_mm,
                power=args.power,
                depth=args.depth,
                mm_per_px=args.mm_per_px,
                wait_per_line=args.wait_per_line,
                min_wait=args.min_wait,
                no_burn=args.no_burn,
            )
            burn_mode = "vector"
            burn_source = f"circle_{args.vector_circle_mm}mm"
        else:
            burn_image(
                ser,
                img,
                args.power,
                args.depth,
                args.wait_per_line,
                args.min_wait,
                args.no_burn,
            )
            burn_mode = "raster"
            if args.pattern:
                burn_source = f"pattern_{args.pattern}"
            else:
                burn_source = args.image if args.image else "unknown"

        # Save config file
        config = {
            "timestamp": burn_start_time.isoformat(),
            "burn_mode": burn_mode,
            "source": burn_source,
            "csv_filename": csv_filename,
            "config_filename": config_filename,
            "port": args.port,
            "power": args.power,
            "depth": args.depth,
            "threshold": args.threshold,
            "max_width": args.max_width,
            "wait_per_line": args.wait_per_line,
            "min_wait": args.min_wait,
            "no_burn": args.no_burn,
        }

        if args.vector_circle_mm:
            config["vector_circle_mm"] = args.vector_circle_mm
            config["mm_per_px"] = args.mm_per_px
        elif args.pattern:
            config["pattern"] = args.pattern
            config["circle_mm"] = args.circle_mm
            config["boundary_mm"] = args.boundary_mm
            config["mm_per_px"] = args.mm_per_px
        elif args.image:
            config["image"] = args.image

        with open(config_filename, "w") as f:
            json.dump(config, f, indent=2)
        print(f"Config saved to: {config_filename}")

    finally:
        ser.close()
        if csv_file:
            csv_file.close()
        if error_log:
            error_log.close()


if __name__ == "__main__":
    main()
