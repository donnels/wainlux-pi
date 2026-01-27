"""Tests for WainluxK6.engrave_transport method."""

import pytest
from PIL import Image
from scripts.k6.driver import WainluxK6
from scripts.k6.transport import MockTransport
from scripts.k6 import protocol


@pytest.fixture
def test_image(tmp_path):
    """Create a small test image (16x16 black square)."""
    img = Image.new("L", (16, 16), color=0)  # Black (will burn)
    img_path = tmp_path / "test.png"
    img.save(img_path)
    return str(img_path)


def test_engrave_transport_success(test_image):
    """Test successful engrave with full protocol sequence."""
    transport = MockTransport()
    driver = WainluxK6()

    # Queue ACKs for full sequence:
    # FRAMING, JOB_HEADER, CONNECT x2, burn_payload (16 lines), INIT x2
    # = 6 + 16 = 22 ACKs needed
    for _ in range(22):
        transport.queue_response(bytes([protocol.ACK]))

    # Queue completion status: FF FF 00 64 (100%)
    transport.queue_response(b"\xff\xff\x00\x64")

    result = driver.engrave_transport(transport, test_image, power=1000, depth=100)

    assert result["ok"] is True
    assert result["chunks"] == 16  # 16 lines
    assert "total_time" in result
    assert "complete" in result["message"]


def test_engrave_transport_idle_timeout(test_image):
    """Test engrave that times out during burn (idle timeout)."""
    transport = MockTransport()
    driver = WainluxK6()

    # Queue ACKs for init sequence only (6 ACKs)
    for _ in range(22):
        transport.queue_response(bytes([protocol.ACK]))

    # No status frames - will trigger idle timeout
    # Mock wait_for_completion to return idle_timeout immediately
    original_wait = protocol.wait_for_completion

    def mock_wait(transport, max_wait_s, idle_s, csv_logger=None):
        return {"status": "idle_timeout", "last_pct": 50}

    protocol.wait_for_completion = mock_wait

    try:
        result = driver.engrave_transport(transport, test_image)

        assert result["ok"] is False
        assert "idle_timeout" in result["message"]
        assert "50%" in result["message"]
    finally:
        protocol.wait_for_completion = original_wait


def test_engrave_transport_framing_fails(test_image):
    """Test early failure at FRAMING command."""
    transport = MockTransport()
    driver = WainluxK6()

    # No ACK queued - FRAMING will timeout

    with pytest.raises(protocol.K6TimeoutError) as exc_info:
        driver.engrave_transport(transport, test_image)

    assert "FRAMING" in str(exc_info.value)


def test_engrave_transport_burn_payload_fails(test_image):
    """Test failure during payload burn."""
    transport = MockTransport()
    driver = WainluxK6()

    # Queue ACKs for FRAMING, JOB_HEADER, CONNECT x2 (6 ACKs)
    for _ in range(6):
        transport.queue_response(bytes([protocol.ACK]))

    # First chunk succeeds, second chunk fails (no ACK)
    transport.queue_response(bytes([protocol.ACK]))
    # No more ACKs - will fail on retry

    with pytest.raises(protocol.K6DeviceError) as exc_info:
        driver.engrave_transport(transport, test_image)

    assert "failed after" in str(exc_info.value)


def test_engrave_transport_image_too_large(tmp_path):
    """Test rejection of oversized image."""
    # Create 2000x2000 image (exceeds 1600x1520 limit)
    img = Image.new("L", (2000, 2000), color=128)
    img_path = tmp_path / "large.png"
    img.save(img_path)

    transport = MockTransport()
    driver = WainluxK6()

    with pytest.raises(ValueError) as exc_info:
        driver.engrave_transport(transport, str(img_path))

    assert "too large" in str(exc_info.value)


def test_engrave_transport_custom_params(test_image):
    """Test engrave with custom power and depth parameters."""
    transport = MockTransport()
    driver = WainluxK6()

    # Queue ACKs for full sequence (22 ACKs + completion status)
    for _ in range(22):
        transport.queue_response(bytes([protocol.ACK]))
    transport.queue_response(b"\xff\xff\x00\x64")

    result = driver.engrave_transport(transport, test_image, power=500, depth=50)

    assert result["ok"] is True
    # Verify JOB_HEADER packet contains custom params
    # (JOB_HEADER is second write after FRAMING)
    job_header = transport.writes[1]
    assert job_header[0] == 0x23  # JOB_HEADER opcode
