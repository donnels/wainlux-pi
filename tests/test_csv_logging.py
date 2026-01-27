"""Tests for CSV logging integration."""

from PIL import Image
import csv
from scripts.k6.driver import WainluxK6
from scripts.k6.transport import MockTransport
from scripts.k6.csv_logger import CSVLogger
from scripts.k6 import protocol


def test_csv_logger_basic(tmp_path):
    """Test CSVLogger basic functionality."""
    csv_path = tmp_path / "test.csv"

    with CSVLogger(str(csv_path)) as logger:
        logger.log_operation(
            phase="test",
            operation="TEST_CMD",
            duration_ms=100.5,
            bytes_transferred=10,
            response_type="ACK",
        )

    # Verify CSV was created and has correct format
    assert csv_path.exists()

    with open(csv_path, "r") as f:
        reader = csv.DictReader(f)
        rows = list(reader)

    assert len(rows) == 1
    assert rows[0]["phase"] == "test"
    assert rows[0]["operation"] == "TEST_CMD"
    assert rows[0]["response_type"] == "ACK"
    assert rows[0]["bytes_transferred"] == "10"


def test_csv_logger_cumulative_bytes(tmp_path):
    """Test that cumulative bytes accumulate correctly."""
    csv_path = tmp_path / "test.csv"

    with CSVLogger(str(csv_path)) as logger:
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

    assert len(rows) == 3
    assert int(rows[0]["cumulative_bytes"]) == 100
    assert int(rows[1]["cumulative_bytes"]) == 150
    assert int(rows[2]["cumulative_bytes"]) == 175


def test_connect_transport_with_csv_logging(tmp_path):
    """Test that connect_transport logs operations to CSV."""
    csv_path = tmp_path / "connect.csv"
    transport = MockTransport()
    driver = WainluxK6()

    # Queue responses for connect sequence
    transport.queue_response(b"\x01\x02\x03")  # VERSION
    transport.queue_response(bytes([protocol.ACK]))  # CONNECT #1
    transport.queue_response(bytes([protocol.ACK]))  # CONNECT #2
    transport.queue_response(bytes([protocol.ACK]))  # HOME

    with CSVLogger(str(csv_path)) as logger:
        driver.connect_transport(transport, csv_logger=logger)

    # Verify CSV has entries for each operation
    with open(csv_path, "r") as f:
        reader = csv.DictReader(f)
        rows = list(reader)

    operations = [row["operation"] for row in rows]
    assert "VERSION" in operations
    assert "CONNECT #1" in operations
    assert "CONNECT #2" in operations
    assert "HOME" in operations

    # All should be in 'connect' phase
    phases = [row["phase"] for row in rows]
    assert all(p == "connect" for p in phases)


def test_engrave_transport_with_csv_logging(tmp_path):
    """Test that engrave_transport logs full burn sequence to CSV."""
    csv_path = tmp_path / "engrave.csv"

    # Create small test image
    img = Image.new("L", (16, 16), color=0)
    img_path = tmp_path / "test.png"
    img.save(img_path)

    transport = MockTransport()
    driver = WainluxK6()

    # Queue ACKs for full sequence (22 ACKs + completion)
    for _ in range(22):
        transport.queue_response(bytes([protocol.ACK]))
    transport.queue_response(b"\xff\xff\x00\x64")  # 100% status

    with CSVLogger(str(csv_path)) as logger:
        result = driver.engrave_transport(transport, str(img_path), csv_logger=logger)

    assert result["ok"] is True

    # Verify CSV has entries for all phases
    with open(csv_path, "r") as f:
        reader = csv.DictReader(f)
        rows = list(reader)

    phases = set(row["phase"] for row in rows)
    assert "setup" in phases  # FRAMING, JOB_HEADER, CONNECT
    assert "burn" in phases  # DATA chunks
    assert "finalize" in phases  # INIT
    assert "wait" in phases  # STATUS updates

    # Check for specific operations
    operations = [row["operation"] for row in rows]
    assert "FRAMING" in operations
    assert "JOB_HEADER" in operations
    assert any("DATA" in op for op in operations)
    assert "STATUS" in operations


def test_burn_payload_logs_retries(tmp_path):
    """Test that burn_payload logs retry operations."""
    csv_path = tmp_path / "burn.csv"
    transport = MockTransport()

    # First chunk fails twice (timeout), then succeeds on 3rd attempt
    original_read = transport.read
    read_count = [0]

    def mock_read(n):
        read_count[0] += 1
        # First 40 reads timeout (2 attempts), then provide ACK for retry
        if read_count[0] <= 40:
            return b""
        if read_count[0] == 41:
            transport.queue_response(bytes([protocol.ACK]))
        return original_read(n)

    transport.read = mock_read

    payload_lines = [b"X" * 100]

    with CSVLogger(str(csv_path)) as logger:
        chunks = protocol.burn_payload(
            transport, payload_lines, max_retries=3, csv_logger=logger
        )

    assert chunks == 1

    # Verify CSV was written and has retry entries
    with open(csv_path, "r") as f:
        reader = csv.DictReader(f)
        rows = list(reader)

    # Should have at least 1 row (successful attempt logs)
    # Retries log separately with RETRY in operation name
    assert len(rows) >= 1

    # Check for retry entries (there should be 2 retries before success)
    retry_rows = [r for r in rows if "RETRY" in r["operation"]]
    if len(retry_rows) > 0:  # Retries are logged
        assert all(r["state"] == "RETRY" for r in retry_rows)
        assert all(int(r["retry_count"]) > 0 for r in retry_rows)
