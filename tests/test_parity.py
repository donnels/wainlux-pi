"""Parity tests comparing legacy k6_burn_image.py with new library.

Tests run without hardware by mocking transport/serial access.
Compares protocol sequences, CSV format, and image processing.
"""

import pytest
from pathlib import Path
from PIL import Image
import csv
import tempfile
import sys

# Add scripts to path
sys.path.insert(0, str(Path(__file__).parent.parent / "scripts"))

from k6.driver import WainluxK6
from k6.transport import MockTransport
from k6.csv_logger import CSVLogger
from k6 import protocol
from k6.protocol import K6DeviceError


@pytest.fixture
def test_image_16x16(tmp_path):
    """Create 16x16 test image (black square)."""
    img = Image.new("L", (16, 16), color=0)
    img_path = tmp_path / "test_16x16.png"
    img.save(img_path)
    return str(img_path)


@pytest.fixture
def test_image_100x100(tmp_path):
    """Create 100x100 test image with pattern."""
    img = Image.new("L", (100, 100), color=255)
    # Draw black square in center
    for y in range(25, 75):
        for x in range(25, 75):
            img.putpixel((x, y), 0)
    img_path = tmp_path / "test_100x100.png"
    img.save(img_path)
    return str(img_path)


def test_csv_format_matches_legacy():
    """Verify CSV column headers match legacy format exactly."""
    legacy_headers = [
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

    with tempfile.NamedTemporaryFile(mode="w", suffix=".csv", delete=False) as tmp:
        csv_path = tmp.name

    try:
        # Create CSV with new library
        with CSVLogger(csv_path) as logger:
            logger.log_operation(
                phase="test", operation="TEST", duration_ms=100, bytes_transferred=10
            )

        # Read and verify headers
        with open(csv_path, "r") as f:
            reader = csv.reader(f)
            headers = next(reader)

        assert headers == legacy_headers, "CSV headers don't match legacy format"
    finally:
        Path(csv_path).unlink(missing_ok=True)


def test_connect_sequence_protocol_order():
    """Verify connect sequence matches legacy script order: STOP→VERSION→CONNECT×2→HOME."""
    transport = MockTransport()
    driver = WainluxK6()

    # Queue responses for connect sequence
    # STOP doesn't expect a response
    transport.queue_response(b"\x01\x02\x03")  # VERSION
    transport.queue_response(bytes([protocol.ACK]))  # CONNECT #1
    transport.queue_response(bytes([protocol.ACK]))  # CONNECT #2
    transport.queue_response(bytes([protocol.ACK]))  # HOME

    driver.connect_transport(transport)

    # Verify command sequence: STOP(0x16) → VERSION(0xFF) → CONNECT(0x0A) × 2 → HOME(0x17)
    assert (
        len(transport.writes) >= 5
    ), f"Expected at least 5 commands, got {len(transport.writes)}"

    # Check STOP command (0x16) first
    assert transport.writes[0][0] == 0x16, "First command should be STOP"

    # Check VERSION command (0xFF) second
    assert transport.writes[1][0] == 0xFF, "Second command should be VERSION"

    # Check CONNECT commands (0x0A) appear twice
    connect_cmds = [w for w in transport.writes if w[0] == 0x0A]
    assert len(connect_cmds) == 2, "Should have 2 CONNECT commands"

    # Check HOME command (0x17) last
    home_cmds = [w for w in transport.writes if w[0] == 0x17]
    assert len(home_cmds) == 1, "Should have 1 HOME command"


def test_image_processing_payload_format(test_image_16x16):
    """Verify image is converted to packed pixels matching legacy format."""
    transport = MockTransport()
    driver = WainluxK6()

    # Queue responses for full sequence
    for _ in range(22):  # FRAMING, JOB_HEADER, CONNECT×2, DATA×16, INIT×2
        transport.queue_response(bytes([protocol.ACK]))
    transport.queue_response(b"\xff\xff\x00\x64")  # 100% status

    result = driver.engrave_transport(
        transport, test_image_16x16, power=1000, depth=100
    )

    assert result["ok"] is True

    # Find DATA packets (opcode 0x22)
    data_packets = [w for w in transport.writes if w[0] == 0x22]

    # 16x16 image = 16 lines, each line = 2 bytes packed (16 pixels / 8)
    assert len(data_packets) == 16, f"Expected 16 DATA packets, got {len(data_packets)}"

    # Each DATA packet should have:
    # opcode (1) + length (2) + payload (2 bytes for 16px line) + checksum (1) = 6 bytes
    for pkt in data_packets:
        assert len(pkt) == 6, f"DATA packet wrong size: {len(pkt)}"
        assert pkt[0] == 0x22, "Should be DATA opcode"


def test_job_header_format():
    """Verify JOB_HEADER packet structure matches legacy format."""
    # Build JOB_HEADER for 100×100 image
    header = protocol.build_job_header_raster(
        width=100, height=100, depth=50, power=500
    )

    # Legacy format: 38 bytes total
    assert len(header) == 38, f"JOB_HEADER should be 38 bytes, got {len(header)}"

    # Check opcode
    assert header[0] == 0x23, "JOB_HEADER opcode should be 0x23"

    # Check length field
    assert header[1] == 0x00 and header[2] == 38, "Length field should be 38"

    # Check raster dimensions (big-endian at offsets 6-9)
    width_be = (header[6] << 8) | header[7]
    height_be = (header[8] << 8) | header[9]
    assert width_be == 100, f"Width should be 100, got {width_be}"
    assert height_be == 100, f"Height should be 100, got {height_be}"

    # Check power (big-endian at offsets 12-13)
    power_be = (header[12] << 8) | header[13]
    assert power_be == 500, f"Power should be 500, got {power_be}"

    # Check depth (big-endian at offsets 14-15)
    depth_be = (header[14] << 8) | header[15]
    assert depth_be == 50, f"Depth should be 50, got {depth_be}"


def test_checksum_calculation():
    """Verify checksum matches legacy two's complement algorithm."""
    # Test with known values from legacy script
    test_data = bytes([0x22, 0x00, 0x10, 0xAA, 0xBB, 0xCC])

    # Build packet with checksum
    pkt = bytearray(7)
    pkt[0:6] = test_data
    pkt[6] = protocol.checksum(pkt)

    # Verify checksum formula: (-sum(bytes[:-1])) & 0xFF
    expected = (-sum(test_data)) & 0xFF
    assert pkt[6] == expected, f"Checksum mismatch: got {pkt[6]}, expected {expected}"


def test_data_chunk_size():
    """Verify DATA_CHUNK constant matches legacy value."""
    assert protocol.DATA_CHUNK == 1900, "DATA_CHUNK should be 1900 bytes"


def test_ack_constant():
    """Verify ACK byte matches legacy value."""
    assert protocol.ACK == 0x09, "ACK should be 0x09"


def test_heartbeat_constant():
    """Verify HEARTBEAT frame matches legacy value."""
    assert protocol.HEARTBEAT == b"\xff\xff\xff\xfe", "HEARTBEAT should be FF FF FF FE"


def test_csv_logging_phase_names():
    """Verify phase names match legacy script phases."""
    legacy_phases = ["connect", "setup", "burn", "finalize", "wait"]

    with tempfile.NamedTemporaryFile(mode="w", suffix=".csv", delete=False) as tmp:
        csv_path = tmp.name

    try:
        with CSVLogger(csv_path) as logger:
            for phase in legacy_phases:
                logger.log_operation(phase=phase, operation="TEST", duration_ms=10)

        with open(csv_path, "r") as f:
            reader = csv.DictReader(f)
            rows = list(reader)

        logged_phases = [row["phase"] for row in rows]
        assert logged_phases == legacy_phases, "Phase names don't match legacy"
    finally:
        Path(csv_path).unlink(missing_ok=True)


def test_csv_logging_response_types():
    """Verify response types match legacy script."""

    # Test parse_response_frames for each type
    test_cases = [
        (bytes([0x09]), "ACK"),
        (b"\xff\xff\xff\xfe", "HEARTBEAT"),
        (b"\xff\xff\xff\xfe\x09", "HEARTBEAT+ACK"),
        (
            b"\xff\xff\x00\x64",
            "OTHER",
        ),  # STATUS is logged differently in wait_for_completion
        (b"", "NONE"),
        (b"\x01\x02\x03", "OTHER"),
    ]

    for rx_bytes, expected_type in test_cases:
        hb, ack, typ = protocol.parse_response_frames(rx_bytes)
        if expected_type != "NONE":  # NONE is replaced with TIMEOUT in send_cmd
            assert (
                typ == expected_type or expected_type == "STATUS"
            ), f"Response type mismatch for {rx_bytes.hex()}: got {typ}, expected {expected_type}"


def test_full_burn_sequence_protocol_order(test_image_16x16, tmp_path):
    """Verify full engrave sequence matches legacy protocol order."""
    transport = MockTransport()
    driver = WainluxK6()
    csv_path = tmp_path / "burn.csv"

    # Queue responses
    for _ in range(22):
        transport.queue_response(bytes([protocol.ACK]))
    transport.queue_response(b"\xff\xff\x00\x64")

    with CSVLogger(str(csv_path)) as logger:
        result = driver.engrave_transport(
            transport, test_image_16x16, power=1000, depth=100, csv_logger=logger
        )

    assert result["ok"] is True

    # Verify protocol sequence order
    opcodes = [w[0] for w in transport.writes]

    # Expected sequence: FRAMING (0x0B), JOB_HEADER (0x23), CONNECT (0x0A) x2,
    # DATA (0x22) x16, INIT (0x0E) x2
    assert opcodes[0] == 0x0B, "First should be FRAMING"
    assert opcodes[1] == 0x23, "Second should be JOB_HEADER"
    assert opcodes[2] == 0x0A, "Third should be CONNECT #1"
    assert opcodes[3] == 0x0A, "Fourth should be CONNECT #2"

    # Count DATA packets
    data_count = opcodes[4:-2].count(0x22)
    assert data_count == 16, f"Expected 16 DATA packets, got {data_count}"

    # Last two should be INIT
    assert opcodes[-2] == 0x0E, "Second to last should be INIT #1"
    assert opcodes[-1] == 0x0E, "Last should be INIT #2"


def test_csv_cumulative_bytes():
    """Verify cumulative_bytes accumulates correctly like legacy."""
    with tempfile.NamedTemporaryFile(mode="w", suffix=".csv", delete=False) as tmp:
        csv_path = tmp.name

    try:
        with CSVLogger(csv_path) as logger:
            logger.log_operation(
                phase="test", operation="OP1", duration_ms=10, bytes_transferred=100
            )
            logger.log_operation(
                phase="test", operation="OP2", duration_ms=10, bytes_transferred=50
            )
            logger.log_operation(
                phase="test", operation="OP3", duration_ms=10, bytes_transferred=25
            )

        with open(csv_path, "r") as f:
            reader = csv.DictReader(f)
            rows = list(reader)

        assert int(rows[0]["cumulative_bytes"]) == 100
        assert int(rows[1]["cumulative_bytes"]) == 150
        assert int(rows[2]["cumulative_bytes"]) == 175
    finally:
        Path(csv_path).unlink(missing_ok=True)


def test_image_dimensions_limits():
    """Verify image size limits match legacy (1600x1520)."""
    # This should succeed
    header_ok = protocol.build_job_header_raster(
        width=1600, height=1520, depth=100, power=1000
    )
    assert header_ok is not None

    # Test driver rejects oversized images
    with tempfile.NamedTemporaryFile(suffix=".png", delete=False) as tmp:
        img = Image.new("L", (2000, 2000), color=128)
        img.save(tmp.name)
        tmp_path = tmp.name

    try:
        transport = MockTransport()
        driver = WainluxK6()

        with pytest.raises(ValueError) as exc_info:
            driver.engrave_transport(transport, tmp_path)

        assert "too large" in str(exc_info.value).lower()
    finally:
        Path(tmp_path).unlink(missing_ok=True)


def test_retry_backoff_timing():
    """Verify exponential backoff logic matches legacy (0.1s, 0.2s, 0.4s)."""
    import time

    transport = MockTransport()

    # Track write times (each write = new attempt after failure)
    attempt_times = []
    original_write = transport.write

    def track_write(data):
        attempt_times.append(time.time())
        return original_write(data)

    transport.write = track_write

    # Always timeout (never provide data)
    def always_timeout(n):
        time.sleep(0.01)  # Small sleep to avoid busy loop
        return b""

    transport.read = always_timeout
    transport.set_timeout(0.05)  # Very short timeout for faster test

    payload_lines = [b"X" * 100]

    # Should fail after 3 attempts with backoffs between them
    try:
        protocol.burn_payload(transport, payload_lines, max_retries=3)
        assert False, "Should have raised K6DeviceError"
    except K6DeviceError:
        pass  # Expected

    # Should have 3 attempts (initial + 2 retries)
    assert len(attempt_times) == 3, f"Expected 3 attempts, got {len(attempt_times)}"

    # Check that delays increase exponentially
    # First retry delay should be less than second retry delay
    delay1 = attempt_times[1] - attempt_times[0]
    delay2 = attempt_times[2] - attempt_times[1]

    # Verify exponential pattern: delay2 should be roughly 2x delay1
    # (allowing for timeout overhead which is constant across both)
    # The backoff doubles: 0.1s → 0.2s, so delay2 should be greater
    assert (
        delay2 > delay1
    ), f"Second retry delay ({delay2:.3f}s) should be > first ({delay1:.3f}s)"

    # The pure backoff values are 0.1s and 0.2s, so difference should be ~0.1s
    # (after accounting for constant timeout in both delays)
    backoff_diff = delay2 - delay1
    assert (
        0.05 <= backoff_diff <= 0.15
    ), f"Backoff increase should be ~0.1s, got {backoff_diff:.3f}s"


def test_payload_packing_msb_first():
    """Verify pixel packing is MSB-first matching legacy."""
    # Create 8-pixel wide black/white pattern: ■□■□■□■□
    img = Image.new("L", (8, 1), color=255)
    for x in [0, 2, 4, 6]:
        img.putpixel((x, 0), 0)  # Black

    with tempfile.NamedTemporaryFile(suffix=".png", delete=False) as tmp:
        img.save(tmp.name)
        tmp_path = tmp.name

    try:
        transport = MockTransport()
        driver = WainluxK6()

        # Queue responses
        for _ in range(7):  # FRAMING, JOB_HEADER, CONNECT×2, DATA×1, INIT×2
            transport.queue_response(bytes([protocol.ACK]))
        transport.queue_response(b"\xff\xff\x00\x64")

        result = driver.engrave_transport(transport, tmp_path)
        assert result["ok"] is True

        # Find DATA packet
        data_packets = [w for w in transport.writes if w[0] == 0x22]
        assert len(data_packets) == 1

        # Extract payload byte (skip opcode, length, get payload before checksum)
        payload_byte = data_packets[0][3]

        # MSB-first: ■□■□■□■□ = 10101010 = 0xAA
        assert (
            payload_byte == 0xAA
        ), f"Expected 0xAA (MSB-first), got 0x{payload_byte:02X}"
    finally:
        Path(tmp_path).unlink(missing_ok=True)


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
